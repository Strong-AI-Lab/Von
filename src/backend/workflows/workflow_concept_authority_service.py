"""Workflow concept authority helpers.

WS1 requires Vontology to be authoritative for workflow identity.  This module
provides one canonical pathway to:
1. bootstrap missing workflow concepts for registered workflows, and
2. classify workflow concept authority drift for parity diagnostics.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services import concept_service
from ..services.concept_service import ConceptNotFoundError
from ..services.effort_unit_ontology_service import ensure_effort_unit_ontology
from ..services.text_value_service import (
    upsert_singleton_text_relation,
    upsert_text_for_concept,
)
from .definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
)
from .parent_specificity_workflow_contracts import (
    PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPING_CONCEPT_ID,
    PARENT_SPECIFICITY_DOSSIER_FAILURE_MODE_BINDINGS,
    PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS,
    PARENT_SPECIFICITY_DOSSIER_WRITES_CONTEXT_KEYS,
    ParentSpecificityToolOutputMappingSpec,
)
from .workflow_gap_workflow_contracts import (
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_COLLECT_CONTEXT_ACTION_ID,
    WORKFLOW_GAP_CREATE_TEST_INPUTS_MAPPING_ID,
    WORKFLOW_GAP_CREATE_TOOL_OUTPUT_MAPPINGS,
    WORKFLOW_GAP_CREATE_WORKFLOW_SPEC_MAPPING_ID,
    WORKFLOW_GAP_CREATE_WRITES_CONTEXT_KEYS,
    WORKFLOW_GAP_CREATION_FAILURE_MODE_BINDINGS,
    WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
    WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
    WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID,
    WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID,
    WORKFLOW_GAP_TEST_ACCEPTANCE_MAPPING_ID,
    WORKFLOW_GAP_TEST_BASE_RESPONSE_MAPPING_ID,
    WORKFLOW_GAP_TEST_FAILURE_MODE_BINDINGS,
    WORKFLOW_GAP_TEST_ORG_CONCEPT_MAPPING_ID,
    WORKFLOW_GAP_TEST_PROMPT_MAPPING_ID,
    WORKFLOW_GAP_TEST_RECENT_TURNS_MAPPING_ID,
    WORKFLOW_GAP_TEST_SESSION_ID_MAPPING_ID,
    WORKFLOW_GAP_TEST_TOOL_OUTPUT_MAPPINGS,
    WORKFLOW_GAP_TEST_TURN_ID_MAPPING_ID,
    WORKFLOW_GAP_TEST_USER_CONCEPT_MAPPING_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID_MAPPING_ID,
    WORKFLOW_GAP_TEST_WRITES_CONTEXT_KEYS,
    WorkflowGapOutputMappingSpec,
)
from .vontology_loader import (
    WORKFLOW_GRAPH_PREDICATE_ALIASES,
    WORKFLOW_STEP_CONTROL_FLOW_CONCEPT_DATA_KEY,
    build_workflow_process_graph,
    load_workflow_definition_from_vontology,
)
from .workflow_action_contracts import (
    WORKFLOW_ACTION_CONTRACT_SPEC_CONCEPT_DATA_KEY,
    WORKFLOW_ACTION_CONTRACT_TEXT_PREDICATE_PRECEDENCE,
    WORKFLOW_ACTION_CONTRACT_TYPE_ID,
    build_workflow_action_contract_payload,
    invalidate_workflow_action_contract_resolution_cache,
)
from .workflow_creation_contracts import (
    WORKFLOW_AUTHORING_ACTION_CONCEPT_DECIDE_REPAIR_OR_CREATE,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_DESIGN_REPAIR_SPEC,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_DISCOVER_EXISTING_WORKFLOWS,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_ENSURE_WORKFLOW_IDENTITY,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_EXTRACT_EXISTING_WORKFLOW_SPEC,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_MATERIALISE_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_PUBLISH_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_VALIDATE_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_DECIDE_REPAIR_OR_CREATE,
    WORKFLOW_AUTHORING_ACTION_DESIGN_REPAIR_SPEC,
    WORKFLOW_AUTHORING_ACTION_DISCOVER_EXISTING_WORKFLOWS,
    WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY,
    WORKFLOW_AUTHORING_ACTION_EXTRACT_EXISTING_WORKFLOW_SPEC,
    WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_PROMPT_REPAIR_OR_CREATE_DECISION,
    WORKFLOW_AUTHORING_PROMPT_REPAIR_SPEC,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_COMPLETED,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_CREATE_NEW_WORKFLOW,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_DECIDE_PATH,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_DISCOVER_EXISTING_WORKFLOWS,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_FAILED,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_REPAIR_EXISTING_WORKFLOW,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_REUSE_EXISTING_WORKFLOW,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID,
    WORKFLOW_AUTHORING_REPAIR_STEP_APPLY_REPAIR,
    WORKFLOW_AUTHORING_REPAIR_STEP_COMPLETED,
    WORKFLOW_AUTHORING_REPAIR_STEP_DESIGN_REPAIR_SPEC,
    WORKFLOW_AUTHORING_REPAIR_STEP_FAILED,
    WORKFLOW_AUTHORING_REPAIR_STEP_LOAD_EXISTING_WORKFLOW_SPEC,
    WORKFLOW_AUTHORING_REPAIR_WORKFLOW_ID,
    WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION,
    WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_STEP_CONCEPTS,
    WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_ACTION_CONCEPT_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_ACTION_CONCEPT_EMIT_MARKER,
    WORKFLOW_CREATION_ACTION_CONCEPT_ESTABLISH_RELATIONSHIPS,
    WORKFLOW_CREATION_ACTION_CONCEPT_FINALISE,
    WORKFLOW_CREATION_ACTION_CONCEPT_IDENTIFY_NEED,
    WORKFLOW_CREATION_ACTION_CONCEPT_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_ACTION_CONTRACT_BY_ACTION_ID,
    WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
    WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_ACTION_EMIT_MARKER,
    WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
    WORKFLOW_CREATION_ACTION_FINALISE,
    WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
    WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS,
    WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
    WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS,
    WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
    WORKFLOW_CREATION_STEP_MATERIALISE_WORKFLOW_DEFINITION,
    WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_WORKFLOW_ID,
)
from .engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
    build_transition_condition,
)
from .workflow_definition_identity_service import (
    collect_workflow_action_ids,
    validate_workflow_definition_contract,
)
from .subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    normalise_subworkflow_contract,
)
from .workflow_registry import WorkflowRegistry
from .write_tool_policy import (
    WORKFLOW_STEP_MUTATION_AUTHORITY_SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)

# Keep durable workflow identity constants local in this module to avoid importing
# ``workflows.durable`` during authority bootstrap (that path imports registry
# factory and can create circular imports).
FILE_COPY_INTERPRETATION_WORKFLOW_ID = "#V#file_copy_interpretation_workflow"
FILE_COPY_TYPING_WORKFLOW_ID = "#V#file_copy_typing_workflow"
FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID = (
    "#V#file_copy_upload_classification_workflow"
)
FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID = "#V#file_copy_upload_handler_workflow"
PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID = (
    "#V#parent_specificity_concept_dossier_workflow"
)
PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID = (
    "#V#parent_specificity_rumination_workflow"
)
RAG_TEXT_RELATION_SYNC_WORKFLOW_ID = "#V#rag_text_relation_sync_workflow"
ENRICHMENT_WORKFLOW_ID = "#V#enrichment_workflow"
WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID = (
    "#V#workflow_introspection_maintenance_workflow"
)
ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID = "#V#entity_identity_resolution_workflow"
JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID = (
    "#V#jira_task_incremental_import_workflow"
)
PLANNING_WORKFLOW_ID = "#V#planning_workflow"
RUMINATION_WORKFLOW_ID = "#V#rumination_workflow"


# Ordered from preferred canonical type to legacy fallbacks.
# Keep all candidates here so workflow typing policy is managed in one place.
WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES: tuple[str, ...] = (
    "#V#ai_workflow",
    "#V#durable_workflow",
    "#V#workflow",
    "#V#llm_workflow",
)


# Canonical workflows that should be represented in Vontology as process graphs.
CANONICAL_CHAT_WORKFLOW_IDS: tuple[str, ...] = (
    MISSING_TOOL_CALL_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_ASSISTANT_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
)

# Additional built-in workflows that should be published when registered.
CANONICAL_DURABLE_WORKFLOW_IDS: tuple[str, ...] = (
    RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
    ENRICHMENT_WORKFLOW_ID,
    FILE_COPY_TYPING_WORKFLOW_ID,
    FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
    WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
    JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
    PLANNING_WORKFLOW_ID,
    PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
    PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
    RUMINATION_WORKFLOW_ID,
)

# Additional Vontology-authored governance workflows that should be repaired to
# canonical executable graph form when present in the registry.
CANONICAL_VONTOLOGY_GOVERNANCE_WORKFLOW_IDS: tuple[str, ...] = (
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID,
    WORKFLOW_AUTHORING_REPAIR_WORKFLOW_ID,
    WORKFLOW_CREATION_WORKFLOW_ID,
)

_CANONICAL_GRAPH_PREDICATES: Dict[str, str] = {
    key: aliases[0] for key, aliases in WORKFLOW_GRAPH_PREDICATE_ALIASES.items()
}
_WORKFLOW_RELATIONSHIP_ALIAS_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInitialStep"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["hasStep"],
        )
    )
)
_STEP_RELATIONSHIP_ALIAS_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["invokesAction"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["invokesWorkflow"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["workflowStepInvokesTool"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInputMap"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES[
                "workflowStepMapsContextKeyToToolParam"
            ],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES[
                "workflowStepMapsToolOutputFieldToContextKey"
            ],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["workflowStepWritesContextKey"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["nextStep"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["onTrueNextStep"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["onFalseNextStep"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["onFailureNextStep"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["onUnknownNextStep"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["onApprovalRequiredNextStep"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["onBreakNextStep"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["onContinueNextStep"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["hasEffect"],
        )
    )
)

_SLUG_SANITISER_RE = re.compile(r"[^a-z0-9_]+")


@dataclass(frozen=True)
class _CanonicalContextInputMappingSpec:
    concept_id: str
    context_key: str
    tool_param: str
    required: bool = True


@dataclass(frozen=True)
class _CanonicalToolOutputMappingSpec:
    concept_id: str
    tool_output_field: str
    context_key: str


@dataclass(frozen=True)
class _CanonicalConditionalTransitionPublicationSpec:
    to_state: str
    condition_spec: Mapping[str, Any]
    reason: str | None = None


@dataclass(frozen=True)
class _CanonicalStepPublicationSpec:
    state_id: str
    concept_id: str | None = None
    action_id: str | None = None
    action_concept_id: str | None = None
    prompt_concept_ids: tuple[str, ...] = ()
    execution_mode: str | None = None
    llm_policy: Mapping[str, Any] | None = None
    validation_policy: Mapping[str, Any] | None = None
    mutation_authority: Mapping[str, Any] | None = None
    invoked_workflow_id: str | None = None
    static_input_bindings: tuple[tuple[str, str], ...] = ()
    context_input_mappings: tuple[str, ...] = ()
    context_input_mapping_specs: tuple[_CanonicalContextInputMappingSpec, ...] = ()
    tool_output_context_mappings: tuple[str, ...] = ()
    tool_output_mapping_specs: tuple[
        ParentSpecificityToolOutputMappingSpec
        | WorkflowGapOutputMappingSpec
        | _CanonicalToolOutputMappingSpec,
        ...,
    ] = ()
    writes_context_keys: tuple[str, ...] = ()
    next_state: str | None = None
    on_true_state: str | None = None
    on_false_state: str | None = None
    on_failure_state: str | None = None
    on_unknown_state: str | None = None
    on_approval_required_state: str | None = None
    on_break_state: str | None = None
    on_continue_state: str | None = None
    conditional_transitions: tuple[
        _CanonicalConditionalTransitionPublicationSpec,
        ...,
    ] = ()
    effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CanonicalWorkflowPublicationSpec:
    initial_state: str
    steps: tuple[_CanonicalStepPublicationSpec, ...]


@dataclass(frozen=True)
class _RuntimeStepPublicationDetails:
    execution_mode: str | None = None
    llm_policy: Mapping[str, Any] | None = None
    validation_policy: Mapping[str, Any] | None = None
    mutation_authority: Mapping[str, Any] | None = None
    invoked_workflow_id: str | None = None
    static_input_bindings: tuple[tuple[str, str], ...] = ()
    context_input_mapping_specs: tuple[_CanonicalContextInputMappingSpec, ...] = ()
    tool_output_mapping_specs: tuple[_CanonicalToolOutputMappingSpec, ...] = ()
    writes_context_keys: tuple[str, ...] = ()


_FILE_COPY_WORKFLOW_CONTEXT_INPUT_MAPPINGS: tuple[str, ...] = (
    "#V#workflow_mapping_concept_id_to_concept_id_parameter",
    "#V#workflow_mapping_file_copy_concept_id_to_file_copy_concept_id_parameter",
)
_PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPINGS: tuple[str, ...] = (
    PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPING_CONCEPT_ID,
)


# Keep this mapping deterministic so publication is stable across runs.
_CANONICAL_WORKFLOW_PUBLICATION_SPECS: Dict[str, _CanonicalWorkflowPublicationSpec] = {
    MISSING_TOOL_CALL_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="observed",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="observed",
                action_id="missing_tool_call.assess",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="needs_retry",
                        reason="retry_needed",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "missing_tool_call_retry_needed",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="no_retry_required",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="needs_retry",
                action_id="missing_tool_call.retry",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="retry_succeeded",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "missing_tool_call_retry_success",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="retry_failed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    CHAT_NARRATION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="classify_need",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="classify_need",
                action_id="narration.classify",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="select_prompt_fragments",
                        reason="narration_required",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "narration_required",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="narration_not_required",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="select_prompt_fragments",
                action_id="narration.select_prompts",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="render_narration",
                        reason="prompt_selected",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="render_narration",
                action_id="narration.render",
                execution_mode="llm",
                llm_policy={
                    "tool_mode": "none",
                    "policy_stage": "narration",
                    "prompt_text_context_key": "narration_prompt_text",
                    "response_contract_text": (
                        "Return ONLY one block: <spoken>...</spoken>. "
                        "Do not include <screen>. Do not include code blocks. "
                        "Use New Zealand English spelling."
                    ),
                    "context_fields": [
                        {"label": "User message", "context_key": "user_prompt"},
                        {
                            "label": (
                                "On-screen content (do not read verbatim if long; "
                                "summarise)"
                            ),
                            "context_key": "screen_text",
                        },
                    ],
                },
                validation_policy={"output_format": "narration_spoken_xml"},
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="emit_audio",
                        reason="rendered",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "narration_rendered",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="render_failed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="emit_audio",
                action_id="narration.emit_audio",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="emitted",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    CHAT_BUTTONIFY_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="assess_input",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="assess_input",
                action_id="buttonify.assess_input",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="select_prompt",
                        reason="eligible",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "buttonify_should_run",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="skipped",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="select_prompt",
                action_id="buttonify.select_prompt",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="extract_options",
                        reason="prompt_selected",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="extract_options",
                action_id="buttonify.extract_options",
                execution_mode="llm",
                llm_policy={
                    "tool_mode": "none",
                    "policy_stage": "buttonify",
                    "prompt_text_context_key": "buttonify_prompt_text",
                    "context_fields": [
                        {"label": "User message", "context_key": "user_prompt"},
                        {
                            "label": "Assistant response",
                            "context_key": "screen_text",
                        },
                    ],
                },
                validation_policy={"output_format": "buttonify_options_json"},
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="transformation_completed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    CHAT_ASSISTANT_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="completed",
        steps=(
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    TODO_REFRESH_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="check_cache_freshness",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="check_cache_freshness",
                action_id="todo_refresh.check_cache",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="maybe_fetch_gmail",
                        reason="refresh_needed",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "todo_refresh_needed",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="cache_fresh",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="maybe_fetch_gmail",
                action_id="todo_refresh.fetch_gmail",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="extract_tasks",
                        reason="gmail_checked",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="extract_tasks",
                action_id="todo_refresh.extract_tasks",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="prioritise",
                        reason="tasks_extracted",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="prioritise",
                action_id="todo_refresh.prioritise",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="summarise",
                        reason="prioritised",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="summarise",
                action_id="todo_refresh.summarise",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="summarised",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    WRITE_TOOL_POLICY_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="decide",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="decide",
                action_id="write_policy.decide",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="approval_required",
                        reason="on_approval_required",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "approval_required",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="decided",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="approval_required"),
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="suggest",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="suggest",
                action_id="preflight.specialised_suggest",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="evaluated",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    TOOL_CALLING_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="preflight_requirements",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="preflight_requirements",
                action_id="tool_calling.preflight_requirements",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="respond",
                        reason="requirements_preflight_completed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="respond",
                action_id="tool_calling.respond",
                execution_mode="llm",
                llm_policy={
                    "tool_mode": "allowed",
                    "policy_stage": "tool_call",
                    "prompt_text_context_key": "prompt",
                },
                mutation_authority={
                    "schema_version": WORKFLOW_STEP_MUTATION_AUTHORITY_SCHEMA_VERSION,
                    "maximum_level": "external_system_guarded",
                },
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="postcondition_critic",
                        reason="response_ready",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="postcondition_critic",
                action_id="turn_execution.critic",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completion_gate",
                        reason="critic_completed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="completion_gate",
                action_id="turn_execution.completion_gate",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="respond",
                        reason="completion_gate_repeat_iteration",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "completion_gate_repeat_iteration",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="follow_up_required",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "completion_gate_requires_follow_up",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="completion_gate_passed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="evaluate",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="evaluate",
                action_id="turn_execution.critic",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="evaluated",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    TURN_COMPLETION_GATE_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="decide",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="decide",
                action_id="turn_execution.completion_gate",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="decided",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="critic",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="critic",
                action_id="turn_execution.critic",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completion_gate",
                        reason="critic_completed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="completion_gate",
                action_id="turn_execution.completion_gate",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="completed",
                        reason="completion_gate_decided",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    FILE_COPY_TYPING_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="infer",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="infer",
                action_id="file_copy_typing.infer",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="persist",
                        reason="typing_inferred",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="persist",
                action_id="file_copy_typing.persist",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="typing_persisted",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="complete"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="classify",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="classify",
                action_id="file_copy_upload.classify",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="persist_decision",
                        reason="classification_completed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="persist_decision",
                action_id="file_copy_upload.persist_decision",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="decision_persisted",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="complete"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="typing",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="typing",
                action_id="workflow_invoke_subworkflow",
                invoked_workflow_id=FILE_COPY_TYPING_WORKFLOW_ID,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="classify",
                        reason="typing_completed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="classify",
                action_id="workflow_invoke_subworkflow",
                invoked_workflow_id=FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="fail_closed",
                        reason="fail_closed_selected",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "upload_fail_closed",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="specialised",
                        reason="specialised_route_selected",
                        condition_spec={
                            "kind": "all",
                            "conditions": [
                                {
                                    "kind": "context_value_equals",
                                    "key": "upload_route_mode",
                                    "value": "specialised",
                                },
                                {
                                    "kind": "context_exists",
                                    "key": "upload_target_workflow_id",
                                    "expected": True,
                                },
                                {
                                    "kind": "not",
                                    "condition": {
                                        "kind": "context_value_equals",
                                        "key": "upload_target_workflow_id",
                                        "value": "",
                                    },
                                },
                            ],
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="interpret",
                        reason="interpret_route_selected",
                        condition_spec={
                            "kind": "context_value_equals",
                            "key": "upload_route_mode",
                            "value": "interpret",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="noop",
                        reason="noop_route_selected",
                        condition_spec={
                            "kind": "context_value_equals",
                            "key": "upload_route_mode",
                            "value": "noop",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="interpret",
                        reason="fallback_interpret_default",
                        condition_spec={
                            "kind": "any",
                            "conditions": [
                                {
                                    "kind": "context_exists",
                                    "key": "upload_allow_interpret_fallback",
                                    "expected": False,
                                },
                                {
                                    "kind": "context_flag",
                                    "key": "upload_allow_interpret_fallback",
                                    "expected": True,
                                },
                            ],
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="noop",
                        reason="fallback_noop_default",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="specialised",
                action_id="workflow_invoke_subworkflow",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="specialised_failed",
                        reason="specialised_child_failed",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "upload_specialised_child_failed",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="record_outcome",
                        reason="specialised_completed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="specialised_failed",
                action_id="file_copy_upload.mark_specialised_failure",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="interpret",
                        reason="fallback_to_interpret",
                        condition_spec={
                            "kind": "any",
                            "conditions": [
                                {
                                    "kind": "context_exists",
                                    "key": "upload_allow_interpret_fallback",
                                    "expected": False,
                                },
                                {
                                    "kind": "context_flag",
                                    "key": "upload_allow_interpret_fallback",
                                    "expected": True,
                                },
                            ],
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="record_outcome",
                        reason="no_fallback_after_specialised_failure",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="interpret",
                action_id="workflow_invoke_subworkflow",
                invoked_workflow_id=FILE_COPY_INTERPRETATION_WORKFLOW_ID,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="record_outcome",
                        reason="interpret_completed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="noop",
                action_id="file_copy_upload.mark_noop",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="record_outcome",
                        reason="noop_recorded",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="fail_closed",
                action_id="file_copy_upload.mark_fail_closed",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="record_outcome",
                        reason="fail_closed_recorded",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="record_outcome",
                action_id="file_copy_upload.persist_route_outcome",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="outcome_persisted",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="complete"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    FILE_COPY_INTERPRETATION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="interpret",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="interpret",
                action_id="interpret_file_copy",
                context_input_mappings=_FILE_COPY_WORKFLOW_CONTEXT_INPUT_MAPPINGS,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="index",
                        reason="index_enabled",
                        condition_spec={
                            "kind": "any",
                            "conditions": [
                                {
                                    "kind": "context_exists",
                                    "key": "index_in_rag",
                                    "expected": False,
                                },
                                {
                                    "kind": "context_flag",
                                    "key": "index_in_rag",
                                    "expected": True,
                                },
                            ],
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="index_skipped",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="index",
                action_id="index_file_copy",
                context_input_mappings=_FILE_COPY_WORKFLOW_CONTEXT_INPUT_MAPPINGS,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="index_finished",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="complete"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="collect",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="collect",
                action_id="parent_specificity.collect_dossier",
                writes_context_keys=(
                    "concept_dossier",
                    "concept_dossier_summary",
                    "concept_dossier_languages",
                    "concept_dossier_relation_count",
                ),
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="dossier_collected",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="complete"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="assess",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="assess",
                action_id="parent_specificity.assess_candidates",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="prepare_candidate",
                        reason="candidates_found",
                        condition_spec={
                            "kind": "context_cardinality",
                            "key": "candidate_ids",
                            "operator": "gte",
                            "value": 1,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="no_candidates",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="prepare_candidate",
                action_id="parent_specificity.prepare_candidate",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="gather_dossier",
                        reason="candidate_selected",
                        condition_spec={
                            "kind": "all",
                            "conditions": [
                                {
                                    "kind": "context_exists",
                                    "key": "current_candidate_id",
                                    "expected": True,
                                },
                                {
                                    "kind": "not",
                                    "condition": {
                                        "kind": "context_is_null",
                                        "key": "current_candidate_id",
                                        "expected": True,
                                    },
                                },
                                {
                                    "kind": "not",
                                    "condition": {
                                        "kind": "context_value_equals",
                                        "key": "current_candidate_id",
                                        "value": "",
                                    },
                                },
                            ],
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="no_remaining_candidates",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="gather_dossier",
                action_id="workflow_invoke_subworkflow",
                invoked_workflow_id=PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
                static_input_bindings=PARENT_SPECIFICITY_DOSSIER_FAILURE_MODE_BINDINGS,
                context_input_mappings=_PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPINGS,
                tool_output_context_mappings=tuple(
                    item.concept_id
                    for item in PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS
                ),
                tool_output_mapping_specs=PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS,
                writes_context_keys=PARENT_SPECIFICITY_DOSSIER_WRITES_CONTEXT_KEYS,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="analyse_candidate",
                        reason="dossier_ready_or_failed_closed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="analyse_candidate",
                action_id="parent_specificity.analyse_candidate",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="apply_candidate",
                        reason="analysis_complete",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="apply_candidate",
                action_id="parent_specificity.apply_candidate",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="prepare_candidate",
                        reason="more_candidates",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "has_remaining_candidates",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="review_budget_exhausted",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="complete",
                action_id="parent_specificity.finalise",
            ),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="collect_context",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="collect_context",
                action_id=WORKFLOW_GAP_COLLECT_CONTEXT_ACTION_ID,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="analyse_gap",
                        reason="context_collected",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="analyse_gap",
                action_id="workflow_gap.analyse_recovery",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="prepare_candidate",
                        reason="candidate_needed",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "workflow_gap_should_create_candidate",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="no_candidate_needed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="prepare_candidate",
                action_id=WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="create_candidate",
                        reason="candidate_prepared",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="create_candidate",
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                invoked_workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
                static_input_bindings=WORKFLOW_GAP_CREATION_FAILURE_MODE_BINDINGS,
                context_input_mappings=(
                    WORKFLOW_GAP_CREATE_WORKFLOW_SPEC_MAPPING_ID,
                    WORKFLOW_GAP_CREATE_TEST_INPUTS_MAPPING_ID,
                ),
                tool_output_context_mappings=tuple(
                    item.concept_id for item in WORKFLOW_GAP_CREATE_TOOL_OUTPUT_MAPPINGS
                ),
                tool_output_mapping_specs=WORKFLOW_GAP_CREATE_TOOL_OUTPUT_MAPPINGS,
                writes_context_keys=WORKFLOW_GAP_CREATE_WRITES_CONTEXT_KEYS,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="decide_test",
                        reason="candidate_created_or_failed_closed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="decide_test",
                action_id=WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="test_candidate",
                        reason="test_candidate_now",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "workflow_gap_should_test_candidate_now",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="skip_test",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="test_candidate",
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                invoked_workflow_id=WORKFLOW_GAP_TEST_WORKFLOW_ID,
                static_input_bindings=WORKFLOW_GAP_TEST_FAILURE_MODE_BINDINGS,
                context_input_mappings=(
                    WORKFLOW_GAP_TEST_WORKFLOW_ID_MAPPING_ID,
                    WORKFLOW_GAP_TEST_PROMPT_MAPPING_ID,
                    WORKFLOW_GAP_TEST_RECENT_TURNS_MAPPING_ID,
                    WORKFLOW_GAP_TEST_ACCEPTANCE_MAPPING_ID,
                    WORKFLOW_GAP_TEST_BASE_RESPONSE_MAPPING_ID,
                    WORKFLOW_GAP_TEST_USER_CONCEPT_MAPPING_ID,
                    WORKFLOW_GAP_TEST_ORG_CONCEPT_MAPPING_ID,
                    WORKFLOW_GAP_TEST_SESSION_ID_MAPPING_ID,
                    WORKFLOW_GAP_TEST_TURN_ID_MAPPING_ID,
                ),
                tool_output_context_mappings=tuple(
                    item.concept_id for item in WORKFLOW_GAP_TEST_TOOL_OUTPUT_MAPPINGS
                ),
                tool_output_mapping_specs=WORKFLOW_GAP_TEST_TOOL_OUTPUT_MAPPINGS,
                writes_context_keys=WORKFLOW_GAP_TEST_WRITES_CONTEXT_KEYS,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="candidate_test_finished",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="complete",
                action_id=WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
            ),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    WORKFLOW_GAP_TEST_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="run_test",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="run_test",
                action_id=WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID,
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="test_evaluated",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="complete"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    PLANNING_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="assess",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="assess",
                action_id="planning.assess_context",
                on_failure_state="failed",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="infer",
                        reason="context_ready",
                        condition_spec={
                            "kind": "context_exists",
                            "key": "planning_context",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="missing_context",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="infer",
                action_id="planning.infer_plan",
                on_failure_state="failed",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="validate",
                        reason="plan_inferred",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="validate",
                action_id="planning.validate_plan",
                on_failure_state="failed",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="validated",
                        condition_spec={
                            "kind": "context_exists",
                            "key": "planning_validation",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="validation_missing",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="complete",
                action_id="planning.finalise",
            ),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    RUMINATION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="assess",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="assess",
                action_id="rumination.assess_gaps",
                on_true_state="plan",
                on_false_state="complete",
            ),
            _CanonicalStepPublicationSpec(
                state_id="plan",
                action_id="rumination.plan_enrichment",
                on_true_state="dispatch",
                on_false_state="complete",
            ),
            _CanonicalStepPublicationSpec(
                state_id="dispatch",
                action_id="rumination.dispatch_enrichment",
                on_true_state="dispatch",
                on_false_state="complete",
            ),
            _CanonicalStepPublicationSpec(
                state_id="complete",
                action_id="rumination.finalise",
            ),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    RAG_TEXT_RELATION_SYNC_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="collect",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="collect",
                action_id="rag_sync.collect_docs",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="batch",
                        reason="docs_collected",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "collected_docs",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="no_docs_to_sync",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="batch",
                action_id="rag_sync.prepare_batch",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="upsert",
                        reason="batch_prepared",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "current_batch",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="all_batches_processed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="upsert",
                action_id="rag_sync.upsert_batch",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="batch",
                        reason="more_batches",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "has_more_batches",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="all_batches_done",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="complete",
                action_id="rag_sync.finalise",
            ),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    ENRICHMENT_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="collect",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="collect",
                action_id="enrichment.collect_candidates",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="batch",
                        reason="candidates_found",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "candidate_ids",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="no_candidates",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="batch",
                action_id="enrichment.prepare_batch",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="generate",
                        reason="batch_ready",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "current_batch",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="all_processed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="generate",
                action_id="enrichment.process_batch",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="batch",
                        reason="more_batches_pending",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "has_more_batches",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="processing_complete",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="complete",
                action_id="enrichment.finalise",
            ),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="assess",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="assess",
                action_id="workflow_introspection.assess_context",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="on_failure",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "last_action_failed",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="diagnose",
                        reason="evidence_ready",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "maintenance_evidence",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="missing_evidence",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="diagnose",
                action_id="workflow_introspection.diagnose_conflation",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="on_failure",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "last_action_failed",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="plan",
                        reason="diagnosis_ready",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "maintenance_diagnosis",
                            "expected": True,
                        },
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="plan",
                action_id="workflow_introspection.plan_repairs",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="on_failure",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "last_action_failed",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="apply",
                        reason="planned",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="apply",
                action_id="workflow_introspection.apply_repairs",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="on_failure",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "last_action_failed",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="verify",
                        reason="applied_or_skipped",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="verify",
                action_id="workflow_introspection.verify_repairs",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="on_failure",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "last_action_failed",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="verified_or_noop",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="complete",
                action_id="workflow_introspection.finalise",
            ),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="scan",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="scan",
                action_id="identity_resolution.scan_candidates",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="apply",
                        reason="recommendations_ready",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "duplicate_recommendations",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="nothing_to_apply",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="apply",
                action_id="identity_resolution.apply_resolutions",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="apply_complete",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id="complete",
                action_id="identity_resolution.finalise",
            ),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="run_sync",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="run_sync",
                action_id="jira_task_incremental_import.run_sync",
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="failed",
                        reason="sync_failed",
                        condition_spec={
                            "kind": "context_flag",
                            "key": "last_action_failed",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state="complete",
                        reason="sync_complete",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(state_id="complete"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    WORKFLOW_AUTHORING_REPAIR_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state=WORKFLOW_AUTHORING_REPAIR_STEP_LOAD_EXISTING_WORKFLOW_SPEC,
        steps=(
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_STEP_LOAD_EXISTING_WORKFLOW_SPEC,
                concept_id=WORKFLOW_AUTHORING_REPAIR_STEP_LOAD_EXISTING_WORKFLOW_SPEC,
                action_id=WORKFLOW_AUTHORING_ACTION_EXTRACT_EXISTING_WORKFLOW_SPEC,
                action_concept_id=WORKFLOW_AUTHORING_ACTION_CONCEPT_EXTRACT_EXISTING_WORKFLOW_SPEC,
                next_state=WORKFLOW_AUTHORING_REPAIR_STEP_DESIGN_REPAIR_SPEC,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_STEP_DESIGN_REPAIR_SPEC,
                concept_id=WORKFLOW_AUTHORING_REPAIR_STEP_DESIGN_REPAIR_SPEC,
                action_id=WORKFLOW_AUTHORING_ACTION_DESIGN_REPAIR_SPEC,
                action_concept_id=WORKFLOW_AUTHORING_ACTION_CONCEPT_DESIGN_REPAIR_SPEC,
                prompt_concept_ids=(WORKFLOW_AUTHORING_PROMPT_REPAIR_SPEC,),
                execution_mode="llm",
                llm_policy={
                    "context_fields": [
                        {
                            "context_key": "prompt",
                            "label": "Workflow repair request",
                        },
                        {
                            "context_key": "target_workflow_id",
                            "label": "Target workflow ID",
                        },
                        {
                            "context_key": "existing_workflow_spec",
                            "label": "Existing workflow authoring spec",
                        },
                    ],
                    "response_contract_text": (
                        'Return JSON with keys "target_workflow_id", '
                        '"repair_summary", and "repaired_workflow_spec".'
                    ),
                },
                validation_policy={"output_format": "json_value"},
                writes_context_keys=(
                    "target_workflow_id",
                    "workflow_spec",
                    "workflow_repair_summary",
                ),
                tool_output_mapping_specs=(
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_design_to_target_workflow_mapping",
                        tool_output_field="validated_json.target_workflow_id",
                        context_key="target_workflow_id",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_design_to_workflow_spec_mapping",
                        tool_output_field="validated_json.repaired_workflow_spec",
                        context_key="workflow_spec",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_design_to_summary_mapping",
                        tool_output_field="validated_json.repair_summary",
                        context_key="workflow_repair_summary",
                    ),
                ),
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state=WORKFLOW_AUTHORING_REPAIR_STEP_APPLY_REPAIR,
                        reason="repair_spec_ready",
                        condition_spec={
                            "kind": "context_exists",
                            "key": "workflow_spec",
                            "expected": True,
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state=WORKFLOW_AUTHORING_REPAIR_STEP_FAILED,
                        reason="repair_spec_missing",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_STEP_APPLY_REPAIR,
                concept_id=WORKFLOW_AUTHORING_REPAIR_STEP_APPLY_REPAIR,
                action_id="workflow_invoke_subworkflow",
                invoked_workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
                context_input_mapping_specs=(
                    _CanonicalContextInputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_prompt_input_mapping",
                        context_key="prompt",
                        tool_param="prompt",
                        required=False,
                    ),
                    _CanonicalContextInputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_target_workflow_input_mapping",
                        context_key="target_workflow_id",
                        tool_param="target_workflow_id",
                    ),
                    _CanonicalContextInputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_spec_input_mapping",
                        context_key="workflow_spec",
                        tool_param="workflow_spec",
                    ),
                ),
                writes_context_keys=(
                    "response_text",
                    "workflow_concept_id",
                    "workflow_discoverable",
                    "workflow_authoring_repaired_workflow_id",
                ),
                tool_output_mapping_specs=(
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_apply_to_response_mapping",
                        tool_output_field="result.response_text",
                        context_key="response_text",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_apply_to_concept_mapping",
                        tool_output_field="result.workflow_concept_id",
                        context_key="workflow_concept_id",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_apply_to_discoverable_mapping",
                        tool_output_field="result.workflow_discoverable",
                        context_key="workflow_discoverable",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_repair_apply_to_repaired_workflow_mapping",
                        tool_output_field="result.workflow_concept_id",
                        context_key="workflow_authoring_repaired_workflow_id",
                    ),
                ),
                on_failure_state=WORKFLOW_AUTHORING_REPAIR_STEP_FAILED,
                next_state=WORKFLOW_AUTHORING_REPAIR_STEP_COMPLETED,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_STEP_COMPLETED,
                concept_id=WORKFLOW_AUTHORING_REPAIR_STEP_COMPLETED,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_STEP_FAILED,
                concept_id=WORKFLOW_AUTHORING_REPAIR_STEP_FAILED,
            ),
        ),
    ),
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_DISCOVER_EXISTING_WORKFLOWS,
        steps=(
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_DISCOVER_EXISTING_WORKFLOWS,
                concept_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_DISCOVER_EXISTING_WORKFLOWS,
                action_id=WORKFLOW_AUTHORING_ACTION_DISCOVER_EXISTING_WORKFLOWS,
                action_concept_id=WORKFLOW_AUTHORING_ACTION_CONCEPT_DISCOVER_EXISTING_WORKFLOWS,
                static_input_bindings=(
                    ("max_results", "8"),
                    (
                        "exclude_workflow_ids",
                        json.dumps(
                            [
                                WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID,
                                WORKFLOW_AUTHORING_REPAIR_WORKFLOW_ID,
                                WORKFLOW_CREATION_WORKFLOW_ID,
                            ],
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                    ),
                ),
                next_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_DECIDE_PATH,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_DECIDE_PATH,
                concept_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_DECIDE_PATH,
                action_id=WORKFLOW_AUTHORING_ACTION_DECIDE_REPAIR_OR_CREATE,
                action_concept_id=WORKFLOW_AUTHORING_ACTION_CONCEPT_DECIDE_REPAIR_OR_CREATE,
                prompt_concept_ids=(WORKFLOW_AUTHORING_PROMPT_REPAIR_OR_CREATE_DECISION,),
                execution_mode="llm",
                llm_policy={
                    "context_fields": [
                        {
                            "context_key": "workflow_authoring_request_text",
                            "label": "Workflow authoring request",
                        },
                        {
                            "context_key": "workflow_authoring_candidate_workflows",
                            "label": "Candidate workflows",
                        },
                    ],
                    "response_contract_text": (
                        'Return JSON with keys "decision", '
                        '"target_workflow_id", "target_workflow_name", '
                        '"reasoning", "evidence", and "response_text".'
                    ),
                },
                validation_policy={"output_format": "json_value"},
                writes_context_keys=(
                    "workflow_authoring_preflight_decision",
                    "workflow_authoring_target_workflow_id",
                    "workflow_authoring_target_workflow_name",
                    "workflow_authoring_preflight_reasoning",
                    "workflow_authoring_preflight_evidence",
                    "response_text",
                ),
                tool_output_mapping_specs=(
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_decision_mapping",
                        tool_output_field="validated_json.decision",
                        context_key="workflow_authoring_preflight_decision",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_target_workflow_mapping",
                        tool_output_field="validated_json.target_workflow_id",
                        context_key="workflow_authoring_target_workflow_id",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_target_name_mapping",
                        tool_output_field="validated_json.target_workflow_name",
                        context_key="workflow_authoring_target_workflow_name",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_reasoning_mapping",
                        tool_output_field="validated_json.reasoning",
                        context_key="workflow_authoring_preflight_reasoning",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_evidence_mapping",
                        tool_output_field="validated_json.evidence",
                        context_key="workflow_authoring_preflight_evidence",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_response_mapping",
                        tool_output_field="validated_json.response_text",
                        context_key="response_text",
                    ),
                ),
                conditional_transitions=(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_REUSE_EXISTING_WORKFLOW,
                        reason="reuse_existing_workflow",
                        condition_spec={
                            "kind": "context_value_equals",
                            "key": "workflow_authoring_preflight_decision",
                            "value": "reuse",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_REPAIR_EXISTING_WORKFLOW,
                        reason="repair_existing_workflow",
                        condition_spec={
                            "kind": "context_value_equals",
                            "key": "workflow_authoring_preflight_decision",
                            "value": "repair",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_CREATE_NEW_WORKFLOW,
                        reason="create_new_workflow",
                        condition_spec={
                            "kind": "context_value_equals",
                            "key": "workflow_authoring_preflight_decision",
                            "value": "create",
                        },
                    ),
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_FAILED,
                        reason="decision_failed_closed",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_REUSE_EXISTING_WORKFLOW,
                concept_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_REUSE_EXISTING_WORKFLOW,
                action_id=WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                action_concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_EMIT_MARKER,
                static_input_bindings=(
                    ("marker_key", "workflow_concept_id"),
                ),
                context_input_mapping_specs=(
                    _CanonicalContextInputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_reuse_target_input_mapping",
                        context_key="workflow_authoring_target_workflow_id",
                        tool_param="marker_value",
                    ),
                ),
                writes_context_keys=("workflow_concept_id",),
                next_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_COMPLETED,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_REPAIR_EXISTING_WORKFLOW,
                concept_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_REPAIR_EXISTING_WORKFLOW,
                action_id="workflow_invoke_subworkflow",
                invoked_workflow_id=WORKFLOW_AUTHORING_REPAIR_WORKFLOW_ID,
                context_input_mapping_specs=(
                    _CanonicalContextInputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_repair_prompt_input_mapping",
                        context_key="prompt",
                        tool_param="prompt",
                        required=False,
                    ),
                    _CanonicalContextInputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_repair_target_input_mapping",
                        context_key="workflow_authoring_target_workflow_id",
                        tool_param="target_workflow_id",
                    ),
                ),
                writes_context_keys=(
                    "response_text",
                    "workflow_concept_id",
                    "workflow_discoverable",
                    "workflow_authoring_repaired_workflow_id",
                ),
                tool_output_mapping_specs=(
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_repair_to_response_mapping",
                        tool_output_field="result.response_text",
                        context_key="response_text",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_repair_to_concept_mapping",
                        tool_output_field="result.workflow_concept_id",
                        context_key="workflow_concept_id",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_repair_to_discoverable_mapping",
                        tool_output_field="result.workflow_discoverable",
                        context_key="workflow_discoverable",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_repair_to_repaired_id_mapping",
                        tool_output_field="result.workflow_authoring_repaired_workflow_id",
                        context_key="workflow_authoring_repaired_workflow_id",
                    ),
                ),
                on_failure_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_FAILED,
                next_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_COMPLETED,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_CREATE_NEW_WORKFLOW,
                concept_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_CREATE_NEW_WORKFLOW,
                action_id="workflow_invoke_subworkflow",
                invoked_workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
                context_input_mapping_specs=(
                    _CanonicalContextInputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_create_prompt_input_mapping",
                        context_key="prompt",
                        tool_param="prompt",
                        required=False,
                    ),
                    _CanonicalContextInputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_create_spec_input_mapping",
                        context_key="workflow_spec",
                        tool_param="workflow_spec",
                        required=False,
                    ),
                    _CanonicalContextInputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_create_target_input_mapping",
                        context_key="workflow_authoring_target_workflow_id",
                        tool_param="target_workflow_id",
                        required=False,
                    ),
                ),
                writes_context_keys=(
                    "response_text",
                    "workflow_concept_id",
                    "workflow_discoverable",
                ),
                tool_output_mapping_specs=(
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_create_to_response_mapping",
                        tool_output_field="result.response_text",
                        context_key="response_text",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_create_to_concept_mapping",
                        tool_output_field="result.workflow_concept_id",
                        context_key="workflow_concept_id",
                    ),
                    _CanonicalToolOutputMappingSpec(
                        concept_id="#V#workflow_authoring_preflight_create_to_discoverable_mapping",
                        tool_output_field="result.workflow_discoverable",
                        context_key="workflow_discoverable",
                    ),
                ),
                on_failure_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_FAILED,
                next_state=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_COMPLETED,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_COMPLETED,
                concept_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_COMPLETED,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_FAILED,
                concept_id=WORKFLOW_AUTHORING_REPAIR_OR_CREATE_STEP_FAILED,
            ),
        ),
    ),
    WORKFLOW_CREATION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state=WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
        steps=(
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
                concept_id=WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
                action_id=WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
                action_concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_IDENTIFY_NEED,
                next_state=WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
                concept_id=WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
                action_id=WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
                action_concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_DESIGN_STRUCTURE,
                next_state=WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
                concept_id=WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
                action_id=WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY,
                action_concept_id=WORKFLOW_AUTHORING_ACTION_CONCEPT_ENSURE_WORKFLOW_IDENTITY,
                next_state=WORKFLOW_CREATION_STEP_MATERIALISE_WORKFLOW_DEFINITION,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_MATERIALISE_WORKFLOW_DEFINITION,
                concept_id=WORKFLOW_CREATION_STEP_MATERIALISE_WORKFLOW_DEFINITION,
                action_id=WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION,
                action_concept_id=WORKFLOW_AUTHORING_ACTION_CONCEPT_MATERIALISE_WORKFLOW_DEFINITION,
                next_state=WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
                concept_id=WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
                action_id=WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION,
                action_concept_id=WORKFLOW_AUTHORING_ACTION_CONCEPT_VALIDATE_WORKFLOW_DEFINITION,
                next_state=WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
                concept_id=WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
                action_id=WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION,
                action_concept_id=WORKFLOW_AUTHORING_ACTION_CONCEPT_PUBLISH_WORKFLOW_DEFINITION,
            ),
        ),
    ),
}


def _build_publication_transition(
    *,
    to_state: str,
    reason: str,
    condition_spec: Mapping[str, Any],
) -> WorkflowTransitionSpec:
    normalised_spec, compiled_condition = build_transition_condition(condition_spec)
    return WorkflowTransitionSpec(
        to_state=to_state,
        condition=compiled_condition,
        condition_spec=normalised_spec,
        reason=reason,
    )


def _extract_publication_prompt_concept_ids(action: Any) -> tuple[str, ...]:
    prompt_contract = getattr(action, "prompt_contract", None)
    if not isinstance(prompt_contract, Mapping):
        return ()

    prompt_ids: list[str] = []
    resolved_prompt_concept_id = str(
        prompt_contract.get("resolved_prompt_concept_id") or ""
    ).strip()
    if resolved_prompt_concept_id:
        prompt_ids.append(resolved_prompt_concept_id)

    requested_prompt_concept_ids = prompt_contract.get("requested_prompt_concept_ids")
    if isinstance(requested_prompt_concept_ids, Sequence) and not isinstance(
        requested_prompt_concept_ids,
        str,
    ):
        for item in requested_prompt_concept_ids:
            item_text = str(item or "").strip()
            if item_text:
                prompt_ids.append(item_text)
    return tuple(dict.fromkeys(prompt_ids))


def _build_publication_spec_from_definition(
    definition: WorkflowDefinition,
) -> _CanonicalWorkflowPublicationSpec:
    states = getattr(definition, "states", None)
    if not isinstance(states, Mapping) or not states:
        raise ValueError("workflow_definition_states_missing")

    steps: list[_CanonicalStepPublicationSpec] = []
    for state_id, state_spec in states.items():
        state_id_text = str(state_id or "").strip()
        if not state_id_text or not isinstance(state_spec, WorkflowStateSpec):
            continue

        actions = tuple(getattr(state_spec, "actions", ()) or ())
        if len(actions) > 1:
            raise ValueError(
                f"workflow_definition_multiple_actions_unsupported:{definition.workflow_id}:{state_id_text}"
            )
        action = actions[0] if actions else None

        action_id: str | None = None
        action_concept_id: str | None = None
        execution_mode: str | None = None
        invoked_workflow_id: str | None = None
        prompt_concept_ids: tuple[str, ...] = ()
        if action is not None:
            action_id_text = str(getattr(action, "action_id", "") or "").strip()
            action_id = action_id_text or None
            action_concept_id_text = str(
                getattr(action, "contract_concept_id", "") or ""
            ).strip()
            action_concept_id = action_concept_id_text or None
            execution_mode_text = str(
                getattr(action, "execution_mode", "") or ""
            ).strip()
            execution_mode = execution_mode_text or None
            invoked_workflow_id_text = str(
                getattr(action, "subworkflow_id", "") or ""
            ).strip()
            invoked_workflow_id = invoked_workflow_id_text or None
            if invoked_workflow_id and not action_id:
                action_id = WORKFLOW_SUBWORKFLOW_ACTION_ID
            prompt_concept_ids = _extract_publication_prompt_concept_ids(action)

        next_state: str | None = None
        on_true_state: str | None = None
        on_false_state: str | None = None
        on_failure_state: str | None = None
        on_unknown_state: str | None = None
        on_approval_required_state: str | None = None
        on_break_state: str | None = None
        on_continue_state: str | None = None
        conditional_transitions: list[_CanonicalConditionalTransitionPublicationSpec] = []

        for transition in tuple(getattr(state_spec, "transitions", ()) or ()):
            to_state = str(getattr(transition, "to_state", "") or "").strip()
            if not to_state:
                continue
            reason = str(getattr(transition, "reason", "") or "").strip()
            condition_spec = getattr(transition, "condition_spec", None)
            if not isinstance(condition_spec, Mapping):
                condition_spec = {"kind": "always"}
            if reason == "next_step":
                next_state = to_state
            elif reason == "on_true":
                on_true_state = to_state
            elif reason == "on_false":
                on_false_state = to_state
            elif reason == "on_failure":
                on_failure_state = to_state
            elif reason == "on_unknown":
                on_unknown_state = to_state
            elif reason == "on_approval_required":
                on_approval_required_state = to_state
            elif reason == "on_break":
                on_break_state = to_state
            elif reason == "on_continue":
                on_continue_state = to_state
            else:
                conditional_transitions.append(
                    _CanonicalConditionalTransitionPublicationSpec(
                        to_state=to_state,
                        condition_spec=dict(condition_spec),
                        reason=reason or None,
                    )
                )

        steps.append(
            _CanonicalStepPublicationSpec(
                state_id=state_id_text,
                action_id=action_id,
                action_concept_id=action_concept_id,
                prompt_concept_ids=prompt_concept_ids,
                execution_mode=execution_mode,
                invoked_workflow_id=invoked_workflow_id,
                next_state=next_state,
                on_true_state=on_true_state,
                on_false_state=on_false_state,
                on_failure_state=on_failure_state,
                on_unknown_state=on_unknown_state,
                on_approval_required_state=on_approval_required_state,
                on_break_state=on_break_state,
                on_continue_state=on_continue_state,
                conditional_transitions=tuple(conditional_transitions),
            )
        )

    if not steps:
        raise ValueError("workflow_definition_publication_steps_missing")

    initial_state = str(getattr(definition, "initial_state", "") or "").strip()
    if not initial_state:
        initial_state = steps[0].state_id

    return _CanonicalWorkflowPublicationSpec(
        initial_state=initial_state,
        steps=tuple(steps),
    )


def _serialise_conditional_transition_publication_payloads(
    *,
    step: _CanonicalStepPublicationSpec,
    resolve_to_state: Callable[[str], str],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, branch in enumerate(step.conditional_transitions):
        raw_target = str(branch.to_state or "").strip()
        if not raw_target:
            raise ValueError(
                f"workflow_publication_branch_target_missing:{step.state_id}:{index}"
            )
        to_state = resolve_to_state(raw_target)
        reason = str(branch.reason or f"condition_{index + 1}").strip()
        if not reason:
            reason = f"condition_{index + 1}"
        normalised_spec, _compiled_condition = build_transition_condition(
            branch.condition_spec
        )
        payloads.append(
            {
                "to": to_state,
                "reason": reason,
                "condition": normalised_spec,
            }
        )
    return payloads


def _build_definition_from_publication_spec(
    *,
    workflow_id: str,
    spec: _CanonicalWorkflowPublicationSpec,
) -> WorkflowDefinition:
    states: dict[str, WorkflowStateSpec] = {}
    termination_states: list[str] = []
    for step in spec.steps:
        action_inputs: dict[str, Any] = {}
        if step.invoked_workflow_id:
            action_inputs["workflow_id"] = step.invoked_workflow_id
        if step.static_input_bindings:
            action_inputs.update(
                {
                    key: value
                    for key, value in step.static_input_bindings
                    if isinstance(key, str)
                    and key.strip()
                    and isinstance(value, str)
                    and value.strip()
                }
            )
        if step.context_input_mapping_specs:
            for mapping_spec in step.context_input_mapping_specs:
                if not isinstance(mapping_spec.tool_param, str) or not mapping_spec.tool_param.strip():
                    continue
                if (
                    not isinstance(mapping_spec.context_key, str)
                    or not mapping_spec.context_key.strip()
                ):
                    continue
                action_inputs[mapping_spec.tool_param.strip()] = {
                    "$context_key": mapping_spec.context_key.strip(),
                    "$mapping_concept_id": mapping_spec.concept_id,
                }
        actions = (
            (
                WorkflowActionInvocation(
                    action_id=step.action_id,
                    inputs=action_inputs,
                    contract_concept_id=(
                        step.action_concept_id.strip()
                        if isinstance(step.action_concept_id, str)
                        and step.action_concept_id.strip()
                        else None
                    ),
                    execution_mode=step.execution_mode or "deterministic",
                    llm_policy=(
                        dict(step.llm_policy)
                        if isinstance(step.llm_policy, Mapping)
                        else None
                    ),
                    validation_policy=(
                        dict(step.validation_policy)
                        if isinstance(step.validation_policy, Mapping)
                        else None
                    ),
                ),
            )
            if isinstance(step.action_id, str) and step.action_id.strip()
            else ()
        )
        transitions: list[WorkflowTransitionSpec] = []
        for index, branch in enumerate(step.conditional_transitions):
            reason = str(branch.reason or f"condition_{index + 1}").strip()
            if not reason:
                reason = f"condition_{index + 1}"
            transitions.append(
                _build_publication_transition(
                    to_state=branch.to_state,
                    reason=reason,
                    condition_spec=branch.condition_spec,
                )
            )
        if isinstance(step.on_failure_state, str) and step.on_failure_state.strip():
            transitions.append(
                _build_publication_transition(
                    to_state=step.on_failure_state,
                    reason="on_failure",
                    condition_spec={
                        "kind": "context_flag",
                        "key": "last_action_failed",
                        "expected": True,
                    },
                )
            )
        if isinstance(step.on_unknown_state, str) and step.on_unknown_state.strip():
            transitions.append(
                _build_publication_transition(
                    to_state=step.on_unknown_state,
                    reason="on_unknown",
                    condition_spec={
                        "kind": "context_flag",
                        "key": "last_action_unknown",
                        "expected": True,
                    },
                )
            )
        if (
            isinstance(step.on_approval_required_state, str)
            and step.on_approval_required_state.strip()
        ):
            transitions.append(
                _build_publication_transition(
                    to_state=step.on_approval_required_state,
                    reason="on_approval_required",
                    condition_spec={
                        "kind": "context_flag",
                        "key": "approval_required",
                        "expected": True,
                    },
                )
            )
        if isinstance(step.on_break_state, str) and step.on_break_state.strip():
            transitions.append(
                _build_publication_transition(
                    to_state=step.on_break_state,
                    reason="on_break",
                    condition_spec={
                        "kind": "control_signal",
                        "signal": "break",
                    },
                )
            )
        if isinstance(step.on_continue_state, str) and step.on_continue_state.strip():
            transitions.append(
                _build_publication_transition(
                    to_state=step.on_continue_state,
                    reason="on_continue",
                    condition_spec={
                        "kind": "control_signal",
                        "signal": "continue",
                    },
                )
            )
        if isinstance(step.on_true_state, str) and step.on_true_state.strip():
            transitions.append(
                _build_publication_transition(
                    to_state=step.on_true_state,
                    reason="on_true",
                    condition_spec={
                        "kind": "transition_result_truth",
                        "expected": True,
                    },
                )
            )
        if isinstance(step.on_false_state, str) and step.on_false_state.strip():
            transitions.append(
                _build_publication_transition(
                    to_state=step.on_false_state,
                    reason="on_false",
                    condition_spec={
                        "kind": "transition_result_truth",
                        "expected": False,
                    },
                )
            )
        if isinstance(step.next_state, str) and step.next_state.strip():
            transitions.append(
                _build_publication_transition(
                    to_state=step.next_state,
                    reason="next_step",
                    condition_spec={"kind": "always"},
                )
            )
        is_terminal = not transitions
        if is_terminal:
            termination_states.append(step.state_id)
        state_metadata: dict[str, Any] = {}
        if step.invoked_workflow_id:
            state_metadata["invokes_workflow"] = step.invoked_workflow_id
            state_metadata["subworkflow_contract"] = {
                "workflow_id": step.invoked_workflow_id
            }
        if step.context_input_mapping_specs:
            reads_context_keys = [
                mapping_spec.context_key.strip()
                for mapping_spec in step.context_input_mapping_specs
                if isinstance(mapping_spec.context_key, str)
                and mapping_spec.context_key.strip()
                and bool(mapping_spec.required)
            ]
            if reads_context_keys:
                state_metadata["reads_context_keys"] = reads_context_keys
        if step.tool_output_mapping_specs:
            state_metadata["tool_output_context_mappings"] = [
                {
                    "tool_output_field": mapping_spec.tool_output_field,
                    "context_key": mapping_spec.context_key,
                    "mapping_concept_id": mapping_spec.concept_id,
                }
                for mapping_spec in step.tool_output_mapping_specs
                if isinstance(mapping_spec.tool_output_field, str)
                and mapping_spec.tool_output_field.strip()
                and isinstance(mapping_spec.context_key, str)
                and mapping_spec.context_key.strip()
            ]
        if step.writes_context_keys:
            state_metadata["writes_context_keys"] = [
                item.strip()
                for item in step.writes_context_keys
                if isinstance(item, str) and item.strip()
            ]
        if isinstance(step.mutation_authority, Mapping):
            state_metadata["mutation_authority"] = dict(step.mutation_authority)
        states[step.state_id] = WorkflowStateSpec(
            state_id=step.state_id,
            actions=actions,
            transitions=tuple(transitions),
            terminal=is_terminal,
            metadata=state_metadata,
        )
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=spec.initial_state,
        states=states,
        termination_states=tuple(dict.fromkeys(termination_states)),
        purpose=f"Synthetic canonical workflow definition for {workflow_id}.",
    )


def _build_definition_map_from_publication_specs(
    publication_specs: Mapping[str, _CanonicalWorkflowPublicationSpec],
) -> Dict[str, WorkflowDefinition]:
    """Build deterministic synthetic definitions for explicit publication specs."""

    return {
        workflow_id: _build_definition_from_publication_spec(
            workflow_id=workflow_id,
            spec=spec,
        )
        for workflow_id, spec in publication_specs.items()
        if isinstance(workflow_id, str)
        and workflow_id.strip()
        and isinstance(spec, _CanonicalWorkflowPublicationSpec)
    }


def _slugify_token(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("#v#"):
        raw = raw[3:]
    raw = raw.replace("-", "_").replace(".", "_").replace("/", "_").replace(" ", "_")
    raw = _SLUG_SANITISER_RE.sub("_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    return raw or "item"


def _workflow_slug(workflow_id: str) -> str:
    return _slugify_token(workflow_id)


def _resolve_step_invoked_workflow_id(
    *,
    registration_definition: Any,
    state_id: str,
) -> str | None:
    states = getattr(registration_definition, "states", None)
    if not isinstance(states, Mapping):
        return None
    state_spec = states.get(state_id)
    if state_spec is None:
        return None

    metadata = getattr(state_spec, "metadata", None)
    if isinstance(metadata, Mapping):
        contract, _error = normalise_subworkflow_contract(
            metadata.get("subworkflow_contract")
        )
        workflow_id = (
            str((contract or {}).get("workflow_id") or "").strip()
            if isinstance(contract, Mapping)
            else ""
        )
        if workflow_id:
            return workflow_id

        direct_target = str(metadata.get("invokes_workflow") or "").strip()
        if direct_target:
            return direct_target

    actions = getattr(state_spec, "actions", None)
    if not isinstance(actions, Sequence):
        return None
    for action in actions:
        action_id = str(getattr(action, "action_id", "") or "").strip()
        if action_id != "workflow_invoke_subworkflow":
            continue
        inputs = getattr(action, "inputs", None)
        if not isinstance(inputs, Mapping):
            continue
        workflow_input = inputs.get("workflow_id")
        if isinstance(workflow_input, str) and workflow_input.strip():
            return workflow_input.strip()
    return None


def _step_concept_id(*, workflow_id: str, state_id: str) -> str:
    return f"#V#workflow_step_{_workflow_slug(workflow_id)}_{_slugify_token(state_id)}"


def _terminal_effect_id(*, workflow_id: str, state_id: str) -> str:
    return f"#V#workflow_effect_{_workflow_slug(workflow_id)}_{_slugify_token(state_id)}_terminal"


def _workflow_context_key_concept_id(context_key: str) -> str:
    raw = str(context_key or "").strip()
    if raw.startswith("#V#workflow_context_key_"):
        return raw
    if raw.startswith("workflow_context_key_"):
        return f"#V#{raw}"
    if raw.startswith("#V#"):
        return f"#V#workflow_context_key_{raw[3:]}"
    return f"#V#workflow_context_key_{raw}"


def _runtime_context_input_mapping_concept_id(
    *,
    workflow_id: str,
    state_id: str,
    tool_param: str,
    context_key: str,
) -> str:
    return (
        "#V#workflow_mapping_"
        f"{_workflow_slug(workflow_id)}_"
        f"{_slugify_token(state_id)}_"
        f"{_slugify_token(context_key)}_"
        f"to_{_slugify_token(tool_param)}_parameter"
    )


def _runtime_tool_output_mapping_concept_id(
    *,
    workflow_id: str,
    state_id: str,
    tool_output_field: str,
    context_key: str,
) -> str:
    return (
        "#V#workflow_mapping_tool_field_"
        f"{_workflow_slug(workflow_id)}_"
        f"{_slugify_token(state_id)}_"
        f"{_slugify_token(tool_output_field)}_"
        f"to_{_slugify_token(context_key)}"
    )


def _normalise_tool_output_mapping_spec_fields(
    mapping_spec: (
        ParentSpecificityToolOutputMappingSpec
        | WorkflowGapOutputMappingSpec
        | _CanonicalToolOutputMappingSpec
    ),
) -> tuple[str, str, str]:
    return (
        str(getattr(mapping_spec, "concept_id", "") or "").strip(),
        str(getattr(mapping_spec, "tool_output_field", "") or "").strip(),
        str(getattr(mapping_spec, "context_key", "") or "").strip(),
    )


def _extract_runtime_step_publication_details(
    *,
    workflow_id: str,
    state_id: str,
    registration_definition: Any,
) -> _RuntimeStepPublicationDetails:
    states = getattr(registration_definition, "states", None)
    if not isinstance(states, Mapping):
        return _RuntimeStepPublicationDetails()
    state_spec = states.get(state_id)
    if state_spec is None:
        return _RuntimeStepPublicationDetails()

    actions = getattr(state_spec, "actions", None)
    if not isinstance(actions, Sequence):
        return _RuntimeStepPublicationDetails()

    invoked_workflow_id = _resolve_step_invoked_workflow_id(
        registration_definition=registration_definition,
        state_id=state_id,
    )
    static_input_bindings: list[tuple[str, str]] = []
    context_input_mapping_specs: list[_CanonicalContextInputMappingSpec] = []
    execution_mode: str | None = None
    llm_policy: Mapping[str, Any] | None = None
    validation_policy: Mapping[str, Any] | None = None
    mutation_authority: Mapping[str, Any] | None = None

    for action in actions:
        execution_mode = str(getattr(action, "execution_mode", "") or "").strip() or execution_mode
        action_llm_policy = getattr(action, "llm_policy", None)
        if isinstance(action_llm_policy, Mapping) and not isinstance(llm_policy, Mapping):
            llm_policy = dict(action_llm_policy)
        action_validation_policy = getattr(action, "validation_policy", None)
        if isinstance(action_validation_policy, Mapping) and not isinstance(
            validation_policy,
            Mapping,
        ):
            validation_policy = dict(action_validation_policy)
        inputs = getattr(action, "inputs", None)
        if not isinstance(inputs, Mapping):
            continue
        for child_input_key, raw_value in inputs.items():
            child_input_key_text = str(child_input_key or "").strip()
            if not child_input_key_text:
                continue
            if isinstance(raw_value, Mapping):
                context_key = str(raw_value.get("$context_key") or "").strip()
                if not context_key:
                    continue
                mapping_concept_id = str(
                    raw_value.get("$mapping_concept_id") or ""
                ).strip() or _runtime_context_input_mapping_concept_id(
                    workflow_id=workflow_id,
                    state_id=state_id,
                    tool_param=child_input_key_text,
                    context_key=context_key,
                )
                context_input_mapping_specs.append(
                    _CanonicalContextInputMappingSpec(
                        concept_id=mapping_concept_id,
                        context_key=context_key,
                        tool_param=child_input_key_text,
                        required=bool(
                            raw_value.get("$required", raw_value.get("required", True))
                        ),
                    )
                )
                continue
            if not isinstance(raw_value, str):
                continue
            value_text = raw_value.strip()
            if not value_text:
                continue
            if child_input_key_text == "workflow_id" and invoked_workflow_id:
                continue
            if child_input_key_text.startswith("__parent_"):
                continue
            static_input_bindings.append((child_input_key_text, value_text))

    metadata = getattr(state_spec, "metadata", None)
    tool_output_mapping_specs: list[_CanonicalToolOutputMappingSpec] = []
    writes_context_keys: list[str] = []
    if isinstance(metadata, Mapping):
        raw_mutation_authority = metadata.get("mutation_authority")
        if isinstance(raw_mutation_authority, Mapping):
            mutation_authority = dict(raw_mutation_authority)
        raw_tool_output_mappings = metadata.get("tool_output_context_mappings")
        if isinstance(raw_tool_output_mappings, list):
            for item in raw_tool_output_mappings:
                if not isinstance(item, Mapping):
                    continue
                tool_output_field = str(item.get("tool_output_field") or "").strip()
                context_key = str(item.get("context_key") or "").strip()
                if not tool_output_field or not context_key:
                    continue
                mapping_concept_id = str(
                    item.get("mapping_concept_id") or ""
                ).strip() or _runtime_tool_output_mapping_concept_id(
                    workflow_id=workflow_id,
                    state_id=state_id,
                    tool_output_field=tool_output_field,
                    context_key=context_key,
                )
                tool_output_mapping_specs.append(
                    _CanonicalToolOutputMappingSpec(
                        concept_id=mapping_concept_id,
                        tool_output_field=tool_output_field,
                        context_key=context_key,
                    )
                )
        raw_writes_context_keys = metadata.get("writes_context_keys")
        if isinstance(raw_writes_context_keys, list):
            writes_context_keys = [
                str(item or "").strip()
                for item in raw_writes_context_keys
                if isinstance(item, str) and str(item or "").strip()
            ]

    return _RuntimeStepPublicationDetails(
        execution_mode=execution_mode or None,
        llm_policy=dict(llm_policy) if isinstance(llm_policy, Mapping) else None,
        validation_policy=(
            dict(validation_policy)
            if isinstance(validation_policy, Mapping)
            else None
        ),
        mutation_authority=(
            dict(mutation_authority)
            if isinstance(mutation_authority, Mapping)
            else None
        ),
        invoked_workflow_id=invoked_workflow_id or None,
        static_input_bindings=tuple(dict.fromkeys(static_input_bindings)),
        context_input_mapping_specs=tuple(
            {
                (item.concept_id, item.context_key, item.tool_param): item
                for item in context_input_mapping_specs
            }.values()
        ),
        tool_output_mapping_specs=tuple(
            {
                (item.concept_id, item.tool_output_field, item.context_key): item
                for item in tool_output_mapping_specs
            }.values()
        ),
        writes_context_keys=tuple(dict.fromkeys(writes_context_keys)),
    )


def _ensure_context_input_mapping_concept(
    *,
    step_concept_id: str,
    mapping_target_id: str,
    mapping_spec: _CanonicalContextInputMappingSpec,
) -> tuple[bool, str | None]:
    mapping_concept_id = str(mapping_spec.concept_id or "").strip()
    if not mapping_concept_id:
        return False, "mapping_concept_id_missing"

    existing_doc, load_error = _load_concept(mapping_concept_id)
    if load_error:
        return False, f"mapping_lookup_failed:{load_error}"

    created = False
    if existing_doc is None:
        try:
            concept_service.create_concept(
                name=(
                    "Workflow mapping "
                    f"{mapping_spec.context_key} to {mapping_spec.tool_param}"
                ),
                concept_id=mapping_concept_id,
                description=(
                    "Map context key "
                    f"'{mapping_spec.context_key}' to tool parameter "
                    f"'{mapping_spec.tool_param}'."
                ),
                parent_concept_ids=[],
                create_as_instance=True,
            )
            created = True
            existing_doc, load_error = _load_concept(mapping_concept_id)
            if load_error:
                return False, f"mapping_lookup_after_create_failed:{load_error}"
        except Exception as exc:  # pragma: no cover - defensive
            error_text = str(exc)
            if "E11000" in error_text or "duplicate key" in error_text.lower():
                existing_doc, load_error = _load_concept(mapping_concept_id)
                if load_error:
                    return False, f"mapping_lookup_after_duplicate_failed:{load_error}"
                if existing_doc is None:
                    return False, f"mapping_duplicate_without_reload:{mapping_concept_id}"
            else:
                return False, f"mapping_create_failed:{exc}"

    target_spec = {
        "schema_version": 1,
        "mapping_type": "context_key_to_tool_param",
        "workflow_step_id": step_concept_id,
        "tool_id": mapping_target_id,
        "context_key_concept_id": _workflow_context_key_concept_id(
            mapping_spec.context_key
        ),
        "tool_param_name": mapping_spec.tool_param,
        "required": bool(mapping_spec.required),
    }
    existing_spec = (
        ((existing_doc or {}).get("concept_data") or {}).get("workflow_mapping_spec")
        if isinstance(existing_doc, Mapping)
        else None
    )
    if existing_spec == target_spec:
        return False, None

    try:
        concept_service.update_concept(
            mapping_concept_id,
            {"concept_data.workflow_mapping_spec": target_spec},
        )
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"mapping_update_failed:{exc}"
    return created, None


def _ensure_tool_output_mapping_concept(
    *,
    step_concept_id: str,
    mapping_target_id: str,
    mapping_spec: (
        ParentSpecificityToolOutputMappingSpec
        | WorkflowGapOutputMappingSpec
        | _CanonicalToolOutputMappingSpec
    ),
) -> tuple[bool, str | None]:
    mapping_concept_id, tool_output_field, context_key = (
        _normalise_tool_output_mapping_spec_fields(mapping_spec)
    )
    if not mapping_concept_id:
        return False, "mapping_concept_id_missing"

    existing_doc, load_error = _load_concept(mapping_concept_id)
    if load_error:
        return False, f"mapping_lookup_failed:{load_error}"

    created = False
    if existing_doc is None:
        try:
            concept_service.create_concept(
                name=(
                    "Workflow mapping "
                    f"{tool_output_field} to {context_key}"
                ),
                concept_id=mapping_concept_id,
                description=(
                    "Map tool output field "
                    f"'{tool_output_field}' to context key "
                    f"'{context_key}'."
                ),
                parent_concept_ids=[],
                create_as_instance=True,
            )
            created = True
            existing_doc, load_error = _load_concept(mapping_concept_id)
            if load_error:
                return False, f"mapping_lookup_after_create_failed:{load_error}"
        except Exception as exc:  # pragma: no cover - defensive
            error_text = str(exc)
            if "E11000" in error_text or "duplicate key" in error_text.lower():
                existing_doc, load_error = _load_concept(mapping_concept_id)
                if load_error:
                    return False, f"mapping_lookup_after_duplicate_failed:{load_error}"
                if existing_doc is None:
                    return False, f"mapping_duplicate_without_reload:{mapping_concept_id}"
            else:
                return False, f"mapping_create_failed:{exc}"

    target_spec = {
        "schema_version": 1,
        "mapping_type": "tool_output_field_to_context_key",
        "workflow_step_id": step_concept_id,
        "tool_id": mapping_target_id,
        "tool_output_field_name": tool_output_field,
        "target_context_key_concept_id": _workflow_context_key_concept_id(context_key),
    }
    existing_spec = (
        ((existing_doc or {}).get("concept_data") or {}).get("workflow_mapping_spec")
        if isinstance(existing_doc, Mapping)
        else None
    )
    if existing_spec == target_spec:
        return False, None

    try:
        concept_service.update_concept(
            mapping_concept_id,
            {"concept_data.workflow_mapping_spec": target_spec},
        )
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"mapping_update_failed:{exc}"
    return created, None


def _normalise_relationships(concept_doc: Dict[str, Any] | None) -> Dict[str, Any]:
    relationships = concept_doc.get("relationships") if isinstance(concept_doc, dict) else {}
    if not isinstance(relationships, dict):
        return {}
    return dict(relationships)


def _strip_relationship_aliases(
    relationships: Dict[str, Any],
    *,
    alias_keys: tuple[str, ...],
) -> Dict[str, Any]:
    cleaned = dict(relationships)
    for key in alias_keys:
        cleaned.pop(key, None)
    return cleaned


def _ensure_concept_exists(
    *,
    concept_id: str,
    name: str,
    description: str | None = None,
    parent_concept_ids: list[str] | None = None,
) -> tuple[Dict[str, Any] | None, bool, str | None]:
    existing_doc, load_error = _load_concept(concept_id)
    if load_error:
        return None, False, f"lookup_failed:{load_error}"
    if existing_doc is not None:
        return existing_doc, False, None

    try:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            description=description,
            parent_concept_ids=parent_concept_ids or [],
            create_as_instance=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        # Publication is intentionally idempotent. Concurrent/bootstrap races can
        # surface duplicate-key on create even though the concept now exists.
        error_text = str(exc)
        if "E11000" in error_text or "duplicate key" in error_text.lower():
            recovered_doc, recovered_error = _load_concept(concept_id)
            if recovered_error:
                return None, False, f"lookup_after_duplicate_failed:{recovered_error}"
            if recovered_doc is not None:
                return recovered_doc, False, None
        return None, False, f"create_failed:{exc}"

    created_doc, created_error = _load_concept(concept_id)
    if created_error:
        return None, False, f"lookup_after_create_failed:{created_error}"
    return created_doc, True, None


def _ensure_type_concept_exists(
    *,
    concept_id: str,
    name: str,
    description: str | None = None,
) -> tuple[Dict[str, Any] | None, bool, str | None]:
    existing_doc, load_error = _load_concept(concept_id)
    if load_error:
        return None, False, f"lookup_failed:{load_error}"
    if existing_doc is not None:
        return existing_doc, False, None

    try:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            description=description,
            create_as_instance=False,
        )
    except Exception as exc:  # pragma: no cover - defensive
        error_text = str(exc)
        if "E11000" in error_text or "duplicate key" in error_text.lower():
            recovered_doc, recovered_error = _load_concept(concept_id)
            if recovered_error:
                return None, False, f"lookup_after_duplicate_failed:{recovered_error}"
            if recovered_doc is not None:
                return recovered_doc, False, None
        return None, False, f"create_failed:{exc}"

    created_doc, created_error = _load_concept(concept_id)
    if created_error:
        return None, False, f"lookup_after_create_failed:{created_error}"
    return created_doc, True, None


def _ensure_workflow_action_contract_concept(
    *,
    action_id: str,
) -> tuple[str | None, bool, str | None]:
    definition = WORKFLOW_CREATION_ACTION_CONTRACT_BY_ACTION_ID.get(
        str(action_id or "").strip()
    )
    if definition is None:
        return None, False, "action_contract_definition_missing"

    _type_doc, _type_created, type_error = _ensure_type_concept_exists(
        concept_id=WORKFLOW_ACTION_CONTRACT_TYPE_ID,
        name="Workflow Action Contract",
        description=(
            "Canonical action or tool contract concept used by VWL-authored "
            "workflow steps."
        ),
    )
    if type_error:
        return None, False, f"action_contract_type_failed:{type_error}"

    concept_doc, created, create_error = _ensure_concept_exists(
        concept_id=definition.concept_id,
        name=definition.name,
        description=definition.description,
        parent_concept_ids=[WORKFLOW_ACTION_CONTRACT_TYPE_ID],
    )
    if create_error:
        return None, False, create_error

    payload = build_workflow_action_contract_payload(
        concept_id=definition.concept_id,
        action_id=definition.action_id,
        description=definition.description,
        input_schema=definition.input_schema,
        output_schema=definition.output_schema,
        side_effects=definition.side_effects,
        postconditions=definition.postconditions,
    )
    existing_payload = (
        ((concept_doc or {}).get("concept_data") or {}).get(
            WORKFLOW_ACTION_CONTRACT_SPEC_CONCEPT_DATA_KEY
        )
        if isinstance(concept_doc, Mapping)
        else None
    )
    if existing_payload != payload:
        try:
            concept_service.update_concept(
                definition.concept_id,
                {
                    (
                        "concept_data."
                        f"{WORKFLOW_ACTION_CONTRACT_SPEC_CONCEPT_DATA_KEY}"
                    ): payload
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            return None, created, f"action_contract_update_failed:{exc}"

    predicate = WORKFLOW_ACTION_CONTRACT_TEXT_PREDICATE_PRECEDENCE[0][0]
    try:
        upsert_text_for_concept(
            subject_concept_id=definition.concept_id,
            predicate=predicate,
            text=json.dumps(payload, sort_keys=True),
            lang="en-NZ",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return None, created, f"action_contract_text_upsert_failed:{exc}"

    invalidate_workflow_action_contract_resolution_cache()
    return definition.concept_id, created, None


def ensure_workflow_action_contract_concept(
    *,
    action_id: str,
) -> tuple[str | None, bool, str | None]:
    """Ensure a canonical workflow action-contract concept exists.

    This is the reusable VWL authoring primitive used both by canonical
    publication repair and by the workflow-creation workflow when it authors
    new Vontology workflow graphs that bind to concept-backed actions.
    """

    return _ensure_workflow_action_contract_concept(action_id=action_id)


def _invalidate_runnable_verification_for_workflow(
    workflow_id: str,
    *,
    reason: str,
) -> None:
    workflow_id_clean = str(workflow_id or "").strip()
    if not workflow_id_clean:
        return
    try:
        from .durable.workflow_instance_submission_service import (
            invalidate_workflow_runnable_verification_cache,
        )

        invalidate_workflow_runnable_verification_cache(
            reason=reason,
            workflow_id=workflow_id_clean,
        )
    except Exception:
        logger.debug(
            "workflow_authority: runnable verification cache invalidation skipped",
            exc_info=True,
        )


def publish_canonical_chat_workflow_graphs(
    *,
    registry: WorkflowRegistry | None = None,
    create_missing: bool = True,
    target_workflow_ids: Sequence[str] | None = None,
    publication_specs: Mapping[str, _CanonicalWorkflowPublicationSpec] | None = None,
    publication_definitions: Mapping[str, WorkflowDefinition] | None = None,
    publication_purposes: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    """Publish canonical chat workflows as Vontology process graphs.

    The canonical chat workflows remain executable in Python runtime, but their
    workflow topology must be published into Vontology so graph loading and
    introspection surfaces use the same authority.

    ``publication_specs`` / ``publication_definitions`` / ``publication_purposes``
    allow explicit workflow families to publish authoritative graphs without
    temporarily registering built-in runtime workflows.
    """
    explicit_publication_specs: Dict[str, _CanonicalWorkflowPublicationSpec] = {
        str(workflow_id).strip(): spec
        for workflow_id, spec in (publication_specs or {}).items()
        if isinstance(workflow_id, str)
        and str(workflow_id).strip()
        and isinstance(spec, _CanonicalWorkflowPublicationSpec)
    }
    explicit_publication_definitions: Dict[str, WorkflowDefinition] = {
        str(workflow_id).strip(): definition
        for workflow_id, definition in (publication_definitions or {}).items()
        if isinstance(workflow_id, str)
        and str(workflow_id).strip()
        and isinstance(definition, WorkflowDefinition)
    }
    explicit_publication_purposes: Dict[str, str] = {
        str(workflow_id).strip(): str(purpose).strip()
        for workflow_id, purpose in (publication_purposes or {}).items()
        if isinstance(workflow_id, str)
        and str(workflow_id).strip()
        and isinstance(purpose, str)
        and str(purpose).strip()
    }
    available_publication_specs: Dict[str, _CanonicalWorkflowPublicationSpec] = dict(
        _CANONICAL_WORKFLOW_PUBLICATION_SPECS
    )
    available_publication_specs.update(explicit_publication_specs)

    published: list[str] = []
    skipped_missing_registration: list[str] = []
    skipped_missing_concept: list[str] = []
    errors_by_workflow_id: dict[str, str] = {}
    validation_failures_by_workflow_id: dict[str, Dict[str, Any]] = {}
    created_step_ids: list[str] = []
    created_action_concept_ids: list[str] = []
    created_mapping_concept_ids: list[str] = []

    workflow_type_ids = list(resolve_available_workflow_type_ids())
    preferred_workflow_type = workflow_type_ids[0] if workflow_type_ids else None

    if target_workflow_ids is None:
        target_workflow_ids = list(CANONICAL_CHAT_WORKFLOW_IDS)
        if registry is not None:
            for workflow_id in CANONICAL_DURABLE_WORKFLOW_IDS:
                if registry.get_registration(workflow_id) is not None:
                    target_workflow_ids.append(workflow_id)
            for workflow_id in CANONICAL_VONTOLOGY_GOVERNANCE_WORKFLOW_IDS:
                if registry.get_registration(workflow_id) is not None:
                    target_workflow_ids.append(workflow_id)
        for workflow_id in explicit_publication_specs:
            target_workflow_ids.append(workflow_id)
    else:
        target_workflow_ids = [
            str(item).strip()
            for item in target_workflow_ids
            if isinstance(item, str) and str(item).strip()
        ]
    target_workflow_ids = list(dict.fromkeys(target_workflow_ids))
    registry_workflow_ids = (
        {
            str(item).strip()
            for item in registry.all_workflow_ids()
            if isinstance(item, str) and str(item).strip()
        }
        if registry is not None
        else set()
    )
    known_workflow_ids = tuple(
        sorted(
            registry_workflow_ids
            .union(CANONICAL_VONTOLOGY_GOVERNANCE_WORKFLOW_IDS)
            .union(available_publication_specs.keys())
        )
    )

    def _resolve_workflow_definition_for_validation(
        candidate_workflow_id: str,
    ) -> Any | None:
        candidate_id = str(candidate_workflow_id or "").strip()
        if not candidate_id:
            return None
        authoritative_definition = load_workflow_definition_from_vontology(candidate_id)
        if authoritative_definition is not None:
            return authoritative_definition
        explicit_definition = explicit_publication_definitions.get(candidate_id)
        if explicit_definition is not None:
            return explicit_definition
        if registry is not None:
            registered_definition = registry.get(candidate_id)
            if registered_definition is not None:
                return registered_definition
        canonical_spec = available_publication_specs.get(candidate_id)
        if canonical_spec is not None:
            return _build_definition_from_publication_spec(
                workflow_id=candidate_id,
                spec=canonical_spec,
            )
        return None

    for workflow_id in target_workflow_ids:
        spec = available_publication_specs.get(workflow_id)
        registration_definition = explicit_publication_definitions.get(workflow_id)
        workflow_purpose = explicit_publication_purposes.get(workflow_id)
        registration = None
        if registry is not None:
            registration = registry.get_registration(workflow_id)
            if registration is not None:
                registration_definition = registration_definition or getattr(
                    registration,
                    "definition",
                    None,
                )
                if workflow_purpose is None and isinstance(
                    getattr(registration, "purpose", None),
                    str,
                ):
                    workflow_purpose = str(registration.purpose).strip() or None
        if spec is None:
            skipped_missing_registration.append(workflow_id)
            continue
        if registration_definition is None:
            registration_definition = _build_definition_from_publication_spec(
                workflow_id=workflow_id,
                spec=spec,
            )

        workflow_doc, workflow_load_error = _load_concept(workflow_id)
        if workflow_load_error:
            errors_by_workflow_id[workflow_id] = f"workflow_lookup_failed:{workflow_load_error}"
            continue
        if workflow_doc is None:
            if not create_missing:
                skipped_missing_concept.append(workflow_id)
                continue
            workflow_doc, created, create_error = _ensure_concept_exists(
                concept_id=workflow_id,
                name=_titleise_workflow_id(workflow_id),
                description=workflow_purpose,
                parent_concept_ids=_existing_parent_concept_ids(
                    preferred_workflow_type
                ),
            )
            if create_error:
                errors_by_workflow_id[workflow_id] = (
                    f"workflow_create_failed:{create_error}"
                )
                continue
            if created:
                logger.info(
                    "workflow_authority: created canonical workflow concept %s",
                    workflow_id,
                )

        step_id_by_state: Dict[str, str] = {}
        for step in spec.steps:
            explicit_concept_id = (
                step.concept_id.strip()
                if isinstance(step.concept_id, str) and step.concept_id.strip()
                else None
            )
            step_id_by_state[step.state_id] = (
                explicit_concept_id
                if explicit_concept_id is not None
                else _step_concept_id(workflow_id=workflow_id, state_id=step.state_id)
            )
        ordered_step_ids = [step_id_by_state[step.state_id] for step in spec.steps]
        initial_step_id = step_id_by_state.get(spec.initial_state)
        if not initial_step_id:
            errors_by_workflow_id[workflow_id] = "initial_step_not_defined"
            continue

        workflow_relationships = _strip_relationship_aliases(
            _normalise_relationships(workflow_doc),
            alias_keys=_WORKFLOW_RELATIONSHIP_ALIAS_KEYS,
        )
        workflow_relationships[_CANONICAL_GRAPH_PREDICATES["hasInitialStep"]] = [
            initial_step_id
        ]
        workflow_relationships[_CANONICAL_GRAPH_PREDICATES["hasStep"]] = ordered_step_ids

        try:
            concept_service.update_concept(
                workflow_id,
                {"relationships": workflow_relationships},
            )
        except Exception as exc:  # pragma: no cover - defensive
            errors_by_workflow_id[workflow_id] = f"workflow_update_failed:{exc}"
            continue

        step_update_failed = False
        for step in spec.steps:
            step_concept_id = step_id_by_state[step.state_id]
            step_doc, step_load_error = _load_concept(step_concept_id)
            if step_load_error:
                errors_by_workflow_id[workflow_id] = (
                    f"step_lookup_failed:{step_concept_id}:{step_load_error}"
                )
                step_update_failed = True
                break
            if step_doc is None:
                if not create_missing:
                    errors_by_workflow_id[workflow_id] = (
                        f"step_missing:{step_concept_id}"
                    )
                    step_update_failed = True
                    break
                step_doc, step_created, step_create_error = _ensure_concept_exists(
                    concept_id=step_concept_id,
                    name=f"{_titleise_workflow_id(workflow_id)} {step.state_id}",
                    description=(
                        f"Canonical step '{step.state_id}' for workflow {workflow_id}."
                    ),
                )
                if step_create_error:
                    errors_by_workflow_id[workflow_id] = (
                        f"step_create_failed:{step_concept_id}:{step_create_error}"
                    )
                    step_update_failed = True
                    break
                if step_created:
                    created_step_ids.append(step_concept_id)

            step_relationships = _strip_relationship_aliases(
                _normalise_relationships(step_doc),
                alias_keys=_STEP_RELATIONSHIP_ALIAS_KEYS,
            )
            runtime_details = _extract_runtime_step_publication_details(
                workflow_id=workflow_id,
                state_id=step.state_id,
                registration_definition=registration_definition,
            )

            invoked_workflow_id = (
                step.invoked_workflow_id.strip()
                if isinstance(step.invoked_workflow_id, str)
                and step.invoked_workflow_id.strip()
                else runtime_details.invoked_workflow_id
            )
            execution_mode = (
                step.execution_mode.strip()
                if isinstance(step.execution_mode, str) and step.execution_mode.strip()
                else runtime_details.execution_mode
            )
            llm_policy = (
                dict(step.llm_policy)
                if isinstance(step.llm_policy, Mapping)
                else (
                    dict(runtime_details.llm_policy)
                    if isinstance(runtime_details.llm_policy, Mapping)
                    else None
                )
            )
            validation_policy = (
                dict(step.validation_policy)
                if isinstance(step.validation_policy, Mapping)
                else (
                    dict(runtime_details.validation_policy)
                    if isinstance(runtime_details.validation_policy, Mapping)
                    else None
                )
            )
            mutation_authority = (
                dict(step.mutation_authority)
                if isinstance(step.mutation_authority, Mapping)
                else (
                    dict(runtime_details.mutation_authority)
                    if isinstance(runtime_details.mutation_authority, Mapping)
                    else None
                )
            )
            static_input_bindings = (
                step.static_input_bindings
                if step.static_input_bindings
                else runtime_details.static_input_bindings
            )
            context_input_mapping_specs = (
                step.context_input_mapping_specs
                if step.context_input_mapping_specs
                else runtime_details.context_input_mapping_specs
            )
            context_input_mapping_ids = (
                tuple(
                    item.strip()
                    for item in step.context_input_mappings
                    if isinstance(item, str) and item.strip()
                )
                if step.context_input_mappings
                else tuple(item.concept_id for item in context_input_mapping_specs)
            )
            tool_output_mapping_specs = (
                step.tool_output_mapping_specs
                if step.tool_output_mapping_specs
                else runtime_details.tool_output_mapping_specs
            )
            action_concept_id = (
                step.action_concept_id.strip()
                if isinstance(step.action_concept_id, str)
                and step.action_concept_id.strip()
                else None
            )
            if (
                isinstance(step.action_id, str)
                and step.action_id.strip()
                and step.action_id.strip() in WORKFLOW_CREATION_ACTION_CONTRACT_BY_ACTION_ID
            ):
                resolved_action_concept_id, created_action_concept, action_concept_error = (
                    _ensure_workflow_action_contract_concept(
                        action_id=step.action_id.strip()
                    )
                )
                if action_concept_error:
                    errors_by_workflow_id[workflow_id] = (
                        f"action_contract_failed:{step.state_id}:{action_concept_error}"
                    )
                    step_update_failed = True
                    break
                action_concept_id = action_concept_id or resolved_action_concept_id
                if created_action_concept and resolved_action_concept_id:
                    created_action_concept_ids.append(resolved_action_concept_id)
            tool_output_context_mapping_ids = (
                tuple(
                    item.strip()
                    for item in step.tool_output_context_mappings
                    if isinstance(item, str) and item.strip()
                )
                if step.tool_output_context_mappings
                else tuple(
                    _normalise_tool_output_mapping_spec_fields(item)[0]
                    for item in tool_output_mapping_specs
                )
            )
            writes_context_keys = (
                step.writes_context_keys
                if step.writes_context_keys
                else runtime_details.writes_context_keys
            )
            mapping_target_id = (
                invoked_workflow_id
                if invoked_workflow_id
                else (action_concept_id or step.action_id)
            )
            if (
                isinstance(step.action_id, str)
                and step.action_id.strip()
                and not (
                    step.action_id.strip() == WORKFLOW_SUBWORKFLOW_ACTION_ID
                    and invoked_workflow_id
                )
            ):
                step_relationships[_CANONICAL_GRAPH_PREDICATES["invokesAction"]] = [
                    action_concept_id or step.action_id.strip()
                ]
            if invoked_workflow_id:
                step_relationships[
                    _CANONICAL_GRAPH_PREDICATES["invokesWorkflow"]
                ] = [invoked_workflow_id]
            prompt_concept_ids = [
                item.strip()
                for item in step.prompt_concept_ids
                if isinstance(item, str) and item.strip()
            ]
            if prompt_concept_ids:
                step_relationships[
                    _CANONICAL_GRAPH_PREDICATES["workflowStepUsesLlmPrompt"]
                ] = list(dict.fromkeys(prompt_concept_ids))
            if static_input_bindings:
                step_relationships[_CANONICAL_GRAPH_PREDICATES["hasInputMap"]] = [
                    f"{key}={value}"
                    for key, value in static_input_bindings
                    if isinstance(key, str)
                    and key.strip()
                    and isinstance(value, str)
                    and value.strip()
                ]
            runtime_input_maps = list(
                step_relationships.get(_CANONICAL_GRAPH_PREDICATES["hasInputMap"]) or []
            )
            if isinstance(execution_mode, str) and execution_mode.strip():
                runtime_input_maps.append(
                    "workflow_step_execution_mode="
                    f"{execution_mode.strip()}"
                )
            if isinstance(llm_policy, Mapping) and llm_policy:
                runtime_input_maps.append(
                    "workflow_step_llm_policy="
                    + json.dumps(dict(llm_policy), ensure_ascii=True, sort_keys=True)
                )
            if isinstance(validation_policy, Mapping) and validation_policy:
                runtime_input_maps.append(
                    "workflow_step_validation_policy="
                    + json.dumps(
                        dict(validation_policy),
                        ensure_ascii=True,
                        sort_keys=True,
                    )
                )
            if runtime_input_maps:
                step_relationships[_CANONICAL_GRAPH_PREDICATES["hasInputMap"]] = (
                    list(dict.fromkeys(item for item in runtime_input_maps if item))
                )
            if context_input_mapping_specs:
                if not isinstance(mapping_target_id, str) or not mapping_target_id.strip():
                    errors_by_workflow_id[workflow_id] = (
                        f"context_input_mapping_target_missing:{step_concept_id}"
                    )
                    step_update_failed = True
                    break
                for mapping_spec in context_input_mapping_specs:
                    created_mapping, mapping_error = _ensure_context_input_mapping_concept(
                        step_concept_id=step_concept_id,
                        mapping_target_id=mapping_target_id.strip(),
                        mapping_spec=mapping_spec,
                    )
                    if mapping_error:
                        errors_by_workflow_id[workflow_id] = (
                            f"context_mapping_spec_failed:{mapping_spec.concept_id}:{mapping_error}"
                        )
                        step_update_failed = True
                        break
                    if created_mapping:
                        created_mapping_concept_ids.append(mapping_spec.concept_id)
                if step_update_failed:
                    break
            if context_input_mapping_ids:
                mapping_ids = list(context_input_mapping_ids)
                if mapping_ids:
                    step_relationships[
                        _CANONICAL_GRAPH_PREDICATES[
                            "workflowStepMapsContextKeyToToolParam"
                        ]
                    ] = mapping_ids
            if tool_output_mapping_specs:
                if not isinstance(mapping_target_id, str) or not mapping_target_id.strip():
                    errors_by_workflow_id[workflow_id] = (
                        f"tool_output_mapping_target_missing:{step_concept_id}"
                    )
                    step_update_failed = True
                    break
                for mapping_spec in tool_output_mapping_specs:
                    mapping_concept_id = _normalise_tool_output_mapping_spec_fields(
                        mapping_spec
                    )[0]
                    created_mapping, mapping_error = _ensure_tool_output_mapping_concept(
                        step_concept_id=step_concept_id,
                        mapping_target_id=mapping_target_id.strip(),
                        mapping_spec=mapping_spec,
                    )
                    if mapping_error:
                        errors_by_workflow_id[workflow_id] = (
                            f"mapping_spec_failed:{mapping_concept_id}:{mapping_error}"
                        )
                        step_update_failed = True
                        break
                    if created_mapping:
                        created_mapping_concept_ids.append(mapping_concept_id)
                if step_update_failed:
                    break
            if tool_output_context_mapping_ids:
                mapping_ids = list(tool_output_context_mapping_ids)
                if mapping_ids:
                    step_relationships[
                        _CANONICAL_GRAPH_PREDICATES[
                            "workflowStepMapsToolOutputFieldToContextKey"
                        ]
                    ] = mapping_ids
            if writes_context_keys:
                writes_context_key_ids = [
                    item.strip()
                    for item in writes_context_keys
                    if isinstance(item, str) and item.strip()
                ]
                if writes_context_key_ids:
                    step_relationships[
                        _CANONICAL_GRAPH_PREDICATES["workflowStepWritesContextKey"]
                    ] = writes_context_key_ids

            if isinstance(step.next_state, str) and step.next_state.strip():
                step_relationships[_CANONICAL_GRAPH_PREDICATES["nextStep"]] = [
                    step_id_by_state[step.next_state]
                ]
            if isinstance(step.on_true_state, str) and step.on_true_state.strip():
                step_relationships[_CANONICAL_GRAPH_PREDICATES["onTrueNextStep"]] = [
                    step_id_by_state[step.on_true_state]
                ]
            if isinstance(step.on_false_state, str) and step.on_false_state.strip():
                step_relationships[_CANONICAL_GRAPH_PREDICATES["onFalseNextStep"]] = [
                    step_id_by_state[step.on_false_state]
                ]
            if isinstance(step.on_failure_state, str) and step.on_failure_state.strip():
                step_relationships[_CANONICAL_GRAPH_PREDICATES["onFailureNextStep"]] = [
                    step_id_by_state[step.on_failure_state]
                ]
            if isinstance(step.on_unknown_state, str) and step.on_unknown_state.strip():
                step_relationships[_CANONICAL_GRAPH_PREDICATES["onUnknownNextStep"]] = [
                    step_id_by_state[step.on_unknown_state]
                ]
            if (
                isinstance(step.on_approval_required_state, str)
                and step.on_approval_required_state.strip()
            ):
                step_relationships[
                    _CANONICAL_GRAPH_PREDICATES["onApprovalRequiredNextStep"]
                ] = [step_id_by_state[step.on_approval_required_state]]
            if isinstance(step.on_break_state, str) and step.on_break_state.strip():
                step_relationships[_CANONICAL_GRAPH_PREDICATES["onBreakNextStep"]] = [
                    step_id_by_state[step.on_break_state]
                ]
            if (
                isinstance(step.on_continue_state, str)
                and step.on_continue_state.strip()
            ):
                step_relationships[
                    _CANONICAL_GRAPH_PREDICATES["onContinueNextStep"]
                ] = [step_id_by_state[step.on_continue_state]]

            effects = list(step.effects)
            if (not step.action_id) and not effects:
                effects = [
                    _terminal_effect_id(
                        workflow_id=workflow_id,
                        state_id=step.state_id,
                    )
                ]
            if effects:
                step_relationships[_CANONICAL_GRAPH_PREDICATES["hasEffect"]] = effects

            step_control_flow_payload = {
                "schema_version": 1,
                "conditions": _serialise_conditional_transition_publication_payloads(
                    step=step,
                    resolve_to_state=lambda state_id: step_id_by_state[state_id],
                ),
            }

            if isinstance(mutation_authority, Mapping):
                try:
                    upsert_singleton_text_relation(
                        subject_concept_id=step_concept_id,
                        predicate="#V#hasWorkflowStepMutationAuthorityJson",
                        text=json.dumps(mutation_authority, sort_keys=True),
                        lang="en-NZ",
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    errors_by_workflow_id[workflow_id] = (
                        "step_mutation_authority_upsert_failed:"
                        f"{step_concept_id}:{exc}"
                    )
                    step_update_failed = True
                    break

            try:
                concept_service.update_concept(
                    step_concept_id,
                    {
                        "relationships": step_relationships,
                        (
                            "concept_data."
                            f"{WORKFLOW_STEP_CONTROL_FLOW_CONCEPT_DATA_KEY}"
                        ): step_control_flow_payload,
                    },
                )
            except Exception as exc:  # pragma: no cover - defensive
                errors_by_workflow_id[workflow_id] = (
                    f"step_update_failed:{step_concept_id}:{exc}"
                )
                step_update_failed = True
                break

        if not step_update_failed:
            registration_source = (
                str(getattr(registration, "source", "") or "").strip().lower()
                if registration is not None
                else ""
            )
            supported_action_ids = (
                collect_workflow_action_ids(registration_definition)
                if registration_definition is not None
                else ()
            )
            enforce_supported_actions = True
            # Vontology-authored workflows can legitimately publish richer action
            # bindings than the currently loaded registration snapshot (for
            # example when bootstrapping an action-less graph to executable form).
            if registration_source == "vontology" and not supported_action_ids:
                enforce_supported_actions = False
            published_definition = load_workflow_definition_from_vontology(workflow_id)
            if published_definition is None:
                errors_by_workflow_id[workflow_id] = (
                    "publication_validation_failed:definition_not_loadable"
                )
                validation_failures_by_workflow_id[workflow_id] = {
                    "errors": ["workflow_definition_not_loadable"],
                    "supported_action_ids": list(supported_action_ids),
                }
                continue

            contract_validation = validate_workflow_definition_contract(
                definition=published_definition,
                supported_action_ids=supported_action_ids,
                enforce_supported_actions=enforce_supported_actions,
                known_workflow_ids=known_workflow_ids,
                workflow_definition_loader=_resolve_workflow_definition_for_validation,
            )
            _graph, graph_warnings = build_workflow_process_graph(workflow_id)
            warning_items = [
                str(item).strip()
                for item in (graph_warnings or [])
                if isinstance(item, str) and str(item).strip()
            ]
            validation_errors = [
                code
                for code in contract_validation.get("errors", [])
                if isinstance(code, str) and code.strip()
            ]
            if warning_items:
                validation_errors.append("workflow_graph_warnings_present")

            if validation_errors:
                validation_failures_by_workflow_id[workflow_id] = {
                    "errors": validation_errors,
                    "graph_warnings": warning_items,
                    "contract_validation": contract_validation,
                    "supported_action_ids": list(supported_action_ids),
                }
                errors_by_workflow_id[workflow_id] = (
                    "publication_validation_failed:"
                    + ",".join(validation_errors)
                )
                continue

            published.append(workflow_id)
            _invalidate_runnable_verification_for_workflow(
                workflow_id,
                reason="workflow_graph_published",
            )

    return {
        "counts": {
            "workflows_targeted": len(target_workflow_ids),
            "workflows_published": len(published),
            "workflows_skipped_missing_registration": len(skipped_missing_registration),
            "workflows_skipped_missing_concept": len(skipped_missing_concept),
            "step_concepts_created": len(created_step_ids),
            "action_concepts_created": len(created_action_concept_ids),
            "mapping_concepts_created": len(created_mapping_concept_ids),
            "validation_failures": len(validation_failures_by_workflow_id),
            "errors": len(errors_by_workflow_id),
        },
        "published_workflow_ids": published,
        "skipped_missing_registration_workflow_ids": skipped_missing_registration,
        "skipped_missing_concept_workflow_ids": skipped_missing_concept,
        "created_step_concept_ids": created_step_ids,
        "created_action_concept_ids": created_action_concept_ids,
        "created_mapping_concept_ids": created_mapping_concept_ids,
        "validation_failures_by_workflow_id": validation_failures_by_workflow_id,
        "errors_by_workflow_id": errors_by_workflow_id,
    }


def publish_workflow_definition_from_definition(
    *,
    definition: WorkflowDefinition,
    create_missing: bool = True,
    purpose: str | None = None,
) -> Dict[str, Any]:
    """Publish one arbitrary workflow definition through the canonical graph path."""

    workflow_id = str(getattr(definition, "workflow_id", "") or "").strip()
    if not workflow_id:
        raise ValueError("workflow_definition_id_missing")

    publication_spec = _build_publication_spec_from_definition(definition)
    publication_purposes = (
        {workflow_id: purpose.strip()}
        if isinstance(purpose, str) and purpose.strip()
        else None
    )
    return publish_canonical_chat_workflow_graphs(
        create_missing=create_missing,
        target_workflow_ids=[workflow_id],
        publication_specs={workflow_id: publication_spec},
        publication_definitions={workflow_id: definition},
        publication_purposes=publication_purposes,
    )


def _titleise_workflow_id(workflow_id: str) -> str:
    slug = workflow_id[3:] if workflow_id.startswith("#V#") else workflow_id
    words = [part for part in slug.replace("-", "_").split("_") if part]
    if not words:
        return workflow_id
    return " ".join(word.capitalize() for word in words)


def _extract_instance_of(concept_doc: Dict[str, Any]) -> List[str]:
    relationships = concept_doc.get("relationships") or {}
    if not isinstance(relationships, dict):
        return []
    raw_values = relationships.get("is_an_instance_of", [])
    if isinstance(raw_values, str):
        return [raw_values] if raw_values else []
    if isinstance(raw_values, list):
        return [item for item in raw_values if isinstance(item, str) and item]
    return []


def _extract_type_parents(concept_doc: Dict[str, Any]) -> List[str]:
    relationships = concept_doc.get("relationships") or {}
    if not isinstance(relationships, dict):
        return []
    raw_values = relationships.get("is_a_type_of", [])
    if isinstance(raw_values, str):
        return [raw_values] if raw_values else []
    if isinstance(raw_values, list):
        return [item for item in raw_values if isinstance(item, str) and item]
    return []


def _load_concept(concept_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    concept_id_clean = str(concept_id or "").strip()
    if not concept_id_clean:
        return None, None
    try:
        return ConceptsRepository.find_one({"concept_id": concept_id_clean}), None
    except ConceptNotFoundError:
        return None, None
    except Exception as exc:  # pragma: no cover - defensive
        return None, str(exc)


def _concept_exists_fast(concept_id: str) -> bool:
    """Return whether a concept exists without triggering alias/name fallback work."""

    concept_id_clean = str(concept_id or "").strip()
    if not concept_id_clean:
        return False
    try:
        return (
            ConceptsRepository.find_one(
                {"concept_id": concept_id_clean},
                {"_id": 1},
            )
            is not None
        )
    except Exception:
        return False


def _instance_of_satisfies_required_types(
    instance_of: List[str],
    *,
    required_type_ids: List[str],
) -> bool:
    """Return whether direct or inherited workflow typing satisfies policy.

    Some workflow concepts are intentionally typed as ``#V#durable_workflow``,
    which is itself a subtype of ``#V#ai_workflow``. Authority checks must
    treat that as valid instead of forcing redundant direct typing writes.
    """

    if not required_type_ids:
        return True

    required = {
        item.strip().lower()
        for item in required_type_ids
        if isinstance(item, str) and item.strip()
    }
    if not required:
        return True

    queue = [
        item.strip()
        for item in instance_of
        if isinstance(item, str) and item.strip()
    ]
    seen: set[str] = set()
    while queue:
        candidate = queue.pop(0)
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        if lowered in required:
            return True

        concept_doc, load_error = _load_concept(candidate)
        if load_error or not isinstance(concept_doc, dict):
            continue
        for parent_id in _extract_type_parents(concept_doc):
            parent_lowered = parent_id.lower()
            if parent_lowered not in seen:
                queue.append(parent_id)
    return False


@lru_cache(maxsize=1)
def resolve_available_workflow_type_ids() -> tuple[str, ...]:
    """Return existing workflow type concepts in canonical preference order."""
    available = [
        type_id
        for type_id in WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES
        if _concept_exists_fast(type_id)
    ]

    if available:
        return tuple(available)

    # Fall back to the preferred canonical candidate so bootstrap can still
    # produce deterministic typing in sparse/dev ontologies.
    return (WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES[0],)


def clear_workflow_type_resolution_cache() -> None:
    """Test helper: clear cached workflow type resolution."""
    resolve_available_workflow_type_ids.cache_clear()


def _existing_parent_concept_ids(*candidate_ids: str | None) -> list[str]:
    """Return only workflow parent concept IDs that already exist."""

    existing: list[str] = []
    seen: set[str] = set()
    for candidate_id in candidate_ids:
        cleaned = str(candidate_id or "").strip()
        if not cleaned or cleaned in seen:
            continue
        if not _concept_exists_fast(cleaned):
            continue
        seen.add(cleaned)
        existing.append(cleaned)
    return existing


def _build_workflow_identity_bootstrap_report(
    *,
    workflow_ids: Sequence[str],
    required_type_ids: Sequence[str],
    preferred_type_id: str | None,
    created: Sequence[str],
    updated: Sequence[str],
    unchanged: Sequence[str],
    errors: Mapping[str, str],
) -> Dict[str, Any]:
    return {
        "counts": {
            "registry_workflows": len(workflow_ids),
            "created": len(created),
            "updated": len(updated),
            "unchanged": len(unchanged),
            "errors": len(errors),
        },
        "required_type_ids": list(required_type_ids),
        "preferred_type_id": preferred_type_id,
        "created_workflow_ids": list(created),
        "updated_workflow_ids": list(updated),
        "unchanged_workflow_ids": list(unchanged),
        "errors_by_workflow_id": dict(errors),
    }


def _build_empty_graph_publication_report(
    *,
    reason: str,
    target_workflow_ids: Sequence[str] | None = None,
) -> Dict[str, Any]:
    target_count = (
        len(
            [
                str(item).strip()
                for item in (target_workflow_ids or ())
                if isinstance(item, str) and str(item).strip()
            ]
        )
        if target_workflow_ids is not None
        else 0
    )
    return {
        "attempted": False,
        "reason": reason,
        "counts": {
            "workflows_targeted": target_count,
            "workflows_published": 0,
            "workflows_skipped_missing_registration": 0,
            "workflows_skipped_missing_concept": 0,
            "step_concepts_created": 0,
            "action_concepts_created": 0,
            "mapping_concepts_created": 0,
            "validation_failures": 0,
            "errors": 0,
        },
        "published_workflow_ids": [],
        "skipped_missing_registration_workflow_ids": [],
        "skipped_missing_concept_workflow_ids": [],
        "created_step_concept_ids": [],
        "created_action_concept_ids": [],
        "created_mapping_concept_ids": [],
        "validation_failures_by_workflow_id": {},
        "errors_by_workflow_id": {},
    }


def _build_empty_effort_unit_ontology_report(*, reason: str) -> Dict[str, Any]:
    return {
        "attempted": False,
        "reason": reason,
        "cached": False,
        "success": True,
        "created_concept_ids": [],
        "updated_concept_ids": [],
        "alignment": {
            "success": True,
            "updated_type_ids": [],
            "unchanged_type_ids": [],
            "skipped_missing_type_ids": [],
            "errors": [],
        },
        "errors": [],
    }


def bootstrap_workflow_concept_identities(
    *,
    registry: WorkflowRegistry,
    create_missing: bool = True,
    enforce_required_type: bool = True,
) -> Dict[str, Any]:
    """Ensure registered workflows have concept identities and required typing."""
    workflow_ids = sorted(set(registry.all_workflow_ids()))
    required_type_ids = list(resolve_available_workflow_type_ids())
    preferred_type_id = required_type_ids[0] if required_type_ids else None

    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    errors: dict[str, str] = {}

    for workflow_id in workflow_ids:
        registration = registry.get_registration(workflow_id)
        concept_doc, load_error = _load_concept(workflow_id)
        if load_error:
            errors[workflow_id] = f"lookup_failed:{load_error}"
            continue

        if concept_doc is None:
            if not create_missing:
                unchanged.append(workflow_id)
                continue

            try:
                parent_ids = _existing_parent_concept_ids(preferred_type_id)
                concept_service.create_concept(
                    name=_titleise_workflow_id(workflow_id),
                    concept_id=workflow_id,
                    description=registration.purpose if registration else None,
                    parent_concept_ids=parent_ids,
                    create_as_instance=True,
                )
                created.append(workflow_id)
                _invalidate_runnable_verification_for_workflow(
                    workflow_id,
                    reason="workflow_concept_created",
                )
            except Exception as exc:  # pragma: no cover - defensive
                errors[workflow_id] = f"create_failed:{exc}"
            continue

        if not enforce_required_type or not required_type_ids:
            unchanged.append(workflow_id)
            continue

        current_instance_of = _extract_instance_of(concept_doc)
        if _instance_of_satisfies_required_types(
            current_instance_of,
            required_type_ids=required_type_ids,
        ):
            unchanged.append(workflow_id)
            continue

        try:
            merged_instance_of = list(current_instance_of)
            if preferred_type_id and preferred_type_id not in merged_instance_of:
                merged_instance_of.append(preferred_type_id)

            relationships = dict(concept_doc.get("relationships") or {})
            relationships["is_an_instance_of"] = merged_instance_of
            concept_service.update_concept(workflow_id, {"relationships": relationships})
            updated.append(workflow_id)
            _invalidate_runnable_verification_for_workflow(
                workflow_id,
                reason="workflow_concept_updated",
            )
        except Exception as exc:  # pragma: no cover - defensive
            errors[workflow_id] = f"type_enforcement_failed:{exc}"

    return _build_workflow_identity_bootstrap_report(
        workflow_ids=workflow_ids,
        required_type_ids=required_type_ids,
        preferred_type_id=preferred_type_id,
        created=created,
        updated=updated,
        unchanged=unchanged,
        errors=errors,
    )


def bootstrap_workflow_concepts(
    *,
    registry: WorkflowRegistry,
    create_missing: bool = True,
    enforce_required_type: bool = True,
    ensure_effort_unit_ontology_bootstrap: bool = True,
    publish_canonical_graphs: bool = True,
    target_workflow_ids: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """Ensure registered workflows have concept identities, typing, and graphs."""
    identity_report = bootstrap_workflow_concept_identities(
        registry=registry,
        create_missing=create_missing,
        enforce_required_type=enforce_required_type,
    )

    effort_unit_ontology_report: dict[str, Any]
    if ensure_effort_unit_ontology_bootstrap:
        try:
            effort_unit_ontology_report = dict(ensure_effort_unit_ontology())
            effort_unit_ontology_report["attempted"] = True
            effort_unit_ontology_report.setdefault(
                "reason",
                (
                    "completed"
                    if bool(effort_unit_ontology_report.get("success"))
                    else "reported_failure"
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive
            effort_unit_ontology_report = {
                "attempted": True,
                "cached": False,
                "success": False,
                "reason": "bootstrap_failed",
                "created_concept_ids": [],
                "updated_concept_ids": [],
                "alignment": {
                    "success": False,
                    "updated_type_ids": [],
                    "unchanged_type_ids": [],
                    "skipped_missing_type_ids": [],
                    "errors": [],
                },
                "errors": [str(exc)],
                "error": str(exc),
            }
    else:
        effort_unit_ontology_report = _build_empty_effort_unit_ontology_report(
            reason="disabled"
        )

    graph_publication_report = _build_empty_graph_publication_report(
        reason="disabled",
        target_workflow_ids=target_workflow_ids,
    )
    if publish_canonical_graphs:
        try:
            graph_publication_report = publish_canonical_chat_workflow_graphs(
                registry=registry,
                create_missing=create_missing,
                target_workflow_ids=target_workflow_ids,
            )
            graph_publication_report["attempted"] = True
            graph_publication_report.setdefault(
                "reason",
                (
                    "completed"
                    if not (graph_publication_report.get("errors_by_workflow_id") or {})
                    else "completed_with_errors"
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive
            graph_publication_report = _build_empty_graph_publication_report(
                reason="publication_exception",
                target_workflow_ids=target_workflow_ids,
            )
            graph_publication_report["attempted"] = True
            graph_publication_report["counts"]["errors"] = 1
            graph_publication_report["errors_by_workflow_id"] = {
                "__publication__": str(exc)
            }

    return {
        **identity_report,
        "graph_publication": graph_publication_report,
        "effort_unit_ontology": effort_unit_ontology_report,
    }


def build_workflow_concept_authority_report(
    *,
    registry: WorkflowRegistry,
) -> Dict[str, Any]:
    """Classify workflow concept authority drift for registered workflows."""
    workflow_ids = sorted(set(registry.all_workflow_ids()))
    required_type_ids = list(resolve_available_workflow_type_ids())

    missing_concepts: list[str] = []
    missing_required_type: dict[str, list[str]] = {}
    lookup_errors: dict[str, str] = {}
    valid: list[str] = []

    for workflow_id in workflow_ids:
        concept_doc, load_error = _load_concept(workflow_id)
        if load_error:
            lookup_errors[workflow_id] = load_error
            continue
        if concept_doc is None:
            missing_concepts.append(workflow_id)
            continue

        instance_of = _extract_instance_of(concept_doc)
        if required_type_ids and not _instance_of_satisfies_required_types(
            instance_of,
            required_type_ids=required_type_ids,
        ):
            missing_required_type[workflow_id] = instance_of
            continue
        valid.append(workflow_id)

    drift_detected = bool(missing_concepts or missing_required_type or lookup_errors)

    return {
        "drift_detected": drift_detected,
        "required_type_ids": required_type_ids,
        "counts": {
            "registry_workflows": len(workflow_ids),
            "missing_concepts": len(missing_concepts),
            "missing_required_type": len(missing_required_type),
            "lookup_errors": len(lookup_errors),
            "valid": len(valid),
        },
        "missing_concept_workflow_ids": missing_concepts,
        "missing_required_type_by_workflow_id": missing_required_type,
        "lookup_errors_by_workflow_id": lookup_errors,
        "valid_workflow_ids": valid,
    }

