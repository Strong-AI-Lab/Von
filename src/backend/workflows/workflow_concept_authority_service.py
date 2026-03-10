"""Workflow concept authority helpers.

WS1 requires Vontology to be authoritative for workflow identity.  This module
provides one canonical pathway to:
1. bootstrap missing workflow concepts for registered workflows, and
2. classify workflow concept authority drift for parity diagnostics.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services import concept_service
from ..services.concept_service import ConceptNotFoundError
from ..services.effort_unit_ontology_service import ensure_effort_unit_ontology
from .definitions import (
    CHAT_NARRATION_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
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
    build_workflow_process_graph,
    load_workflow_definition_from_vontology,
)
from .engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
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
RUMINATION_WORKFLOW_ID = "#V#rumination_workflow"
WORKFLOW_CREATION_WORKFLOW_ID = "#V#von_workflow_creation_workflow"

WORKFLOW_CREATION_STEP_IDENTIFY_NEED = "#V#workflow_creation_step_identify_need"
WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE = "#V#workflow_creation_step_design_structure"
WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE = (
    "#V#workflow_creation_step_create_workflow_type"
)
WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS = (
    "#V#workflow_creation_step_create_step_concepts"
)
WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS = (
    "#V#workflow_creation_step_establish_relationships"
)
WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY = (
    "#V#workflow_creation_step_verify_discoverability"
)
WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA = "#V#workflow_creation_step_document_in_jira"

WORKFLOW_CREATION_ACTION_IDENTIFY_NEED = "workflow_creation.identify_need"
WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE = "workflow_creation.design_structure"
WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE = (
    "workflow_creation.create_workflow_type"
)
WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS = (
    "workflow_creation.create_step_concepts"
)
WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS = (
    "workflow_creation.establish_relationships"
)
WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY = (
    "workflow_creation.verify_discoverability"
)
WORKFLOW_CREATION_ACTION_FINALISE = "workflow_creation.finalise"


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
    TODO_REFRESH_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
)

# Additional built-in workflows that should be published when registered.
CANONICAL_DURABLE_WORKFLOW_IDS: tuple[str, ...] = (
    FILE_COPY_TYPING_WORKFLOW_ID,
    FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
    PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
    PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
    RUMINATION_WORKFLOW_ID,
)

# Additional Vontology-authored governance workflows that should be repaired to
# canonical executable graph form when present in the registry.
CANONICAL_VONTOLOGY_GOVERNANCE_WORKFLOW_IDS: tuple[str, ...] = (
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


@dataclass(frozen=True)
class _CanonicalToolOutputMappingSpec:
    concept_id: str
    tool_output_field: str
    context_key: str


@dataclass(frozen=True)
class _CanonicalStepPublicationSpec:
    state_id: str
    concept_id: str | None = None
    action_id: str | None = None
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
    effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CanonicalWorkflowPublicationSpec:
    initial_state: str
    steps: tuple[_CanonicalStepPublicationSpec, ...]


@dataclass(frozen=True)
class _RuntimeStepPublicationDetails:
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
                on_true_state="needs_retry",
                on_false_state="completed",
            ),
            _CanonicalStepPublicationSpec(
                state_id="needs_retry",
                action_id="missing_tool_call.retry",
                on_true_state="completed",
                on_false_state="failed",
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
                on_true_state="select_prompt_fragments",
                on_false_state="completed",
            ),
            _CanonicalStepPublicationSpec(
                state_id="select_prompt_fragments",
                action_id="narration.select_prompts",
                next_state="render_narration",
            ),
            _CanonicalStepPublicationSpec(
                state_id="render_narration",
                action_id="narration.render",
                on_true_state="emit_audio",
                on_false_state="failed",
            ),
            _CanonicalStepPublicationSpec(
                state_id="emit_audio",
                action_id="narration.emit_audio",
                next_state="completed",
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    TODO_REFRESH_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="check_cache_freshness",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="check_cache_freshness",
                action_id="todo_refresh.check_cache",
                on_true_state="maybe_fetch_gmail",
                on_false_state="completed",
            ),
            _CanonicalStepPublicationSpec(
                state_id="maybe_fetch_gmail",
                action_id="todo_refresh.fetch_gmail",
                next_state="extract_tasks",
            ),
            _CanonicalStepPublicationSpec(
                state_id="extract_tasks",
                action_id="todo_refresh.extract_tasks",
                next_state="prioritise",
            ),
            _CanonicalStepPublicationSpec(
                state_id="prioritise",
                action_id="todo_refresh.prioritise",
                next_state="summarise",
            ),
            _CanonicalStepPublicationSpec(
                state_id="summarise",
                action_id="todo_refresh.summarise",
                next_state="completed",
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
                next_state="completed",
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="suggest",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="suggest",
                action_id="preflight.specialised_suggest",
                next_state="completed",
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
        ),
    ),
    TOOL_CALLING_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="plan",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="plan",
                action_id="tool_calling.plan",
                on_true_state="validate",
                on_false_state="postcondition_critic",
            ),
            _CanonicalStepPublicationSpec(
                state_id="validate",
                action_id="tool_calling.validate",
                on_true_state="execute",
                on_false_state="postcondition_critic",
            ),
            _CanonicalStepPublicationSpec(
                state_id="execute",
                action_id="tool_calling.execute",
                next_state="backfill",
            ),
            _CanonicalStepPublicationSpec(
                state_id="backfill",
                action_id="tool_calling.backfill",
                on_true_state="validate",
                on_false_state="postcondition_critic",
            ),
            _CanonicalStepPublicationSpec(
                state_id="postcondition_critic",
                action_id="turn_execution.critic",
                next_state="completion_gate",
            ),
            _CanonicalStepPublicationSpec(
                state_id="completion_gate",
                action_id="turn_execution.completion_gate",
                # Built-in workflow allows completion-gate retries by
                # transitioning back to plan when repeat_iteration is set.
                on_true_state="plan",
                on_false_state="completed",
            ),
            _CanonicalStepPublicationSpec(state_id="completed"),
            _CanonicalStepPublicationSpec(state_id="failed"),
        ),
    ),
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state="critic",
        steps=(
            _CanonicalStepPublicationSpec(
                state_id="critic",
                action_id="turn_execution.critic",
                next_state="completion_gate",
            ),
            _CanonicalStepPublicationSpec(
                state_id="completion_gate",
                action_id="turn_execution.completion_gate",
                next_state="completed",
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
                context_input_mappings=_FILE_COPY_WORKFLOW_CONTEXT_INPUT_MAPPINGS,
                next_state="persist",
            ),
            _CanonicalStepPublicationSpec(
                state_id="persist",
                action_id="file_copy_typing.persist",
                context_input_mappings=_FILE_COPY_WORKFLOW_CONTEXT_INPUT_MAPPINGS,
                next_state="complete",
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
                next_state="persist_decision",
            ),
            _CanonicalStepPublicationSpec(
                state_id="persist_decision",
                action_id="file_copy_upload.persist_decision",
                next_state="complete",
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
                context_input_mappings=_FILE_COPY_WORKFLOW_CONTEXT_INPUT_MAPPINGS,
                next_state="classify",
            ),
            _CanonicalStepPublicationSpec(
                state_id="classify",
                action_id="workflow_invoke_subworkflow",
                invoked_workflow_id=FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
                on_true_state="specialised",
                on_false_state="interpret",
            ),
            _CanonicalStepPublicationSpec(
                state_id="specialised",
                action_id="workflow_invoke_subworkflow",
                on_true_state="record_outcome",
                on_false_state="specialised_failed",
            ),
            _CanonicalStepPublicationSpec(
                state_id="specialised_failed",
                action_id="file_copy_upload.mark_specialised_failure",
                on_true_state="interpret",
                on_false_state="record_outcome",
            ),
            _CanonicalStepPublicationSpec(
                state_id="interpret",
                action_id="workflow_invoke_subworkflow",
                invoked_workflow_id=FILE_COPY_INTERPRETATION_WORKFLOW_ID,
                next_state="record_outcome",
            ),
            _CanonicalStepPublicationSpec(
                state_id="noop",
                action_id="file_copy_upload.mark_noop",
                next_state="record_outcome",
            ),
            _CanonicalStepPublicationSpec(
                state_id="fail_closed",
                action_id="file_copy_upload.mark_fail_closed",
                next_state="record_outcome",
            ),
            _CanonicalStepPublicationSpec(
                state_id="record_outcome",
                action_id="file_copy_upload.persist_route_outcome",
                next_state="complete",
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
                on_true_state="index",
                on_false_state="complete",
            ),
            _CanonicalStepPublicationSpec(
                state_id="index",
                action_id="index_file_copy",
                context_input_mappings=_FILE_COPY_WORKFLOW_CONTEXT_INPUT_MAPPINGS,
                next_state="complete",
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
                next_state="complete",
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
                on_true_state="prepare_candidate",
                on_false_state="complete",
            ),
            _CanonicalStepPublicationSpec(
                state_id="prepare_candidate",
                action_id="parent_specificity.prepare_candidate",
                on_true_state="gather_dossier",
                on_false_state="complete",
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
                next_state="analyse_candidate",
            ),
            _CanonicalStepPublicationSpec(
                state_id="analyse_candidate",
                action_id="parent_specificity.analyse_candidate",
                next_state="apply_candidate",
            ),
            _CanonicalStepPublicationSpec(
                state_id="apply_candidate",
                action_id="parent_specificity.apply_candidate",
                on_true_state="prepare_candidate",
                on_false_state="complete",
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
                next_state="analyse_gap",
            ),
            _CanonicalStepPublicationSpec(
                state_id="analyse_gap",
                action_id="workflow_gap.analyse_recovery",
                on_true_state="prepare_candidate",
                on_false_state="complete",
            ),
            _CanonicalStepPublicationSpec(
                state_id="prepare_candidate",
                action_id=WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID,
                next_state="create_candidate",
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
                next_state="decide_test",
            ),
            _CanonicalStepPublicationSpec(
                state_id="decide_test",
                action_id=WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
                on_true_state="test_candidate",
                on_false_state="complete",
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
                next_state="complete",
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
                next_state="complete",
            ),
            _CanonicalStepPublicationSpec(state_id="complete"),
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
    WORKFLOW_CREATION_WORKFLOW_ID: _CanonicalWorkflowPublicationSpec(
        initial_state=WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
        steps=(
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
                concept_id=WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
                action_id=WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
                next_state=WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
                concept_id=WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
                action_id=WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
                next_state=WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
                concept_id=WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
                action_id=WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
                next_state=WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS,
                concept_id=WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS,
                action_id=WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
                next_state=WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS,
                concept_id=WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS,
                action_id=WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
                next_state=WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
                concept_id=WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
                action_id=WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
                next_state=WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
            ),
            _CanonicalStepPublicationSpec(
                state_id=WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
                concept_id=WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
                action_id=WORKFLOW_CREATION_ACTION_FINALISE,
            ),
        ),
    ),
}


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
        actions = (
            (
                WorkflowActionInvocation(
                    action_id=step.action_id,
                    inputs=action_inputs,
                ),
            )
            if isinstance(step.action_id, str) and step.action_id.strip()
            else ()
        )
        transitions: list[WorkflowTransitionSpec] = []
        if isinstance(step.next_state, str) and step.next_state.strip():
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=step.next_state,
                    condition=lambda _ctx: True,
                    reason="next",
                )
            )
        if isinstance(step.on_true_state, str) and step.on_true_state.strip():
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=step.on_true_state,
                    condition=lambda ctx: bool(ctx.get("last_action_succeeded")),
                    reason="on_true",
                )
            )
        if isinstance(step.on_false_state, str) and step.on_false_state.strip():
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=step.on_false_state,
                    condition=lambda ctx: bool(ctx.get("last_action_failed")),
                    reason="on_false",
                )
            )
        if isinstance(step.on_failure_state, str) and step.on_failure_state.strip():
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=step.on_failure_state,
                    condition=lambda ctx: bool(ctx.get("last_action_failed")),
                    reason="on_failure",
                )
            )
        if isinstance(step.on_unknown_state, str) and step.on_unknown_state.strip():
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=step.on_unknown_state,
                    condition=lambda ctx: bool(ctx.get("last_action_unknown")),
                    reason="on_unknown",
                )
            )
        is_terminal = not transitions
        if is_terminal:
            termination_states.append(step.state_id)
        states[step.state_id] = WorkflowStateSpec(
            state_id=step.state_id,
            actions=actions,
            transitions=tuple(transitions),
            terminal=is_terminal,
        )
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=spec.initial_state,
        states=states,
        termination_states=tuple(dict.fromkeys(termination_states)),
        purpose=f"Synthetic canonical workflow definition for {workflow_id}.",
    )


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

    for action in actions:
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
    registry: WorkflowRegistry,
    create_missing: bool = True,
    target_workflow_ids: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """Publish canonical chat workflows as Vontology process graphs.

    The canonical chat workflows remain executable in Python runtime, but their
    workflow topology must be published into Vontology so graph loading and
    introspection surfaces use the same authority.
    """
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
        for workflow_id in CANONICAL_DURABLE_WORKFLOW_IDS:
            if registry.get_registration(workflow_id) is not None:
                target_workflow_ids.append(workflow_id)
        for workflow_id in CANONICAL_VONTOLOGY_GOVERNANCE_WORKFLOW_IDS:
            if registry.get_registration(workflow_id) is not None:
                target_workflow_ids.append(workflow_id)
    else:
        target_workflow_ids = [
            str(item).strip()
            for item in target_workflow_ids
            if isinstance(item, str) and str(item).strip()
        ]
    target_workflow_ids = list(dict.fromkeys(target_workflow_ids))
    known_workflow_ids = tuple(
        sorted(
            {
                str(item).strip()
                for item in registry.all_workflow_ids()
                if isinstance(item, str) and str(item).strip()
            }
            .union(CANONICAL_VONTOLOGY_GOVERNANCE_WORKFLOW_IDS)
            .union(_CANONICAL_WORKFLOW_PUBLICATION_SPECS.keys())
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
        registered_definition = registry.get(candidate_id)
        if registered_definition is not None:
            return registered_definition
        canonical_spec = _CANONICAL_WORKFLOW_PUBLICATION_SPECS.get(candidate_id)
        if canonical_spec is not None:
            return _build_definition_from_publication_spec(
                workflow_id=candidate_id,
                spec=canonical_spec,
            )
        return None

    for workflow_id in target_workflow_ids:
        spec = _CANONICAL_WORKFLOW_PUBLICATION_SPECS.get(workflow_id)
        registration = registry.get_registration(workflow_id)
        if spec is None or registration is None:
            skipped_missing_registration.append(workflow_id)
            continue
        registration_definition = getattr(registration, "definition", None)

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
                description=registration.purpose if isinstance(registration.purpose, str) else None,
                parent_concept_ids=[preferred_workflow_type] if preferred_workflow_type else [],
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
                invoked_workflow_id if invoked_workflow_id else step.action_id
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
                    step.action_id.strip()
                ]
            if invoked_workflow_id:
                step_relationships[
                    _CANONICAL_GRAPH_PREDICATES["invokesWorkflow"]
                ] = [invoked_workflow_id]
            if static_input_bindings:
                step_relationships[_CANONICAL_GRAPH_PREDICATES["hasInputMap"]] = [
                    f"{key}={value}"
                    for key, value in static_input_bindings
                    if isinstance(key, str)
                    and key.strip()
                    and isinstance(value, str)
                    and value.strip()
                ]
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

            try:
                concept_service.update_concept(
                    step_concept_id,
                    {"relationships": step_relationships},
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


def bootstrap_workflow_concepts(
    *,
    registry: WorkflowRegistry,
    create_missing: bool = True,
    enforce_required_type: bool = True,
    publish_canonical_graphs: bool = True,
    target_workflow_ids: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """Ensure registered workflows have concept identities, typing, and graphs."""
    effort_unit_ontology_report: dict[str, Any] = {
        "success": False,
        "reason": "not_run",
    }
    try:
        effort_unit_ontology_report = ensure_effort_unit_ontology()
    except Exception as exc:  # pragma: no cover - defensive
        effort_unit_ontology_report = {
            "success": False,
            "reason": "bootstrap_failed",
            "error": str(exc),
        }

    workflow_ids = sorted(set(registry.all_workflow_ids()))
    required_type_ids = list(resolve_available_workflow_type_ids())
    preferred_type_id = required_type_ids[0] if required_type_ids else None

    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    errors: dict[str, str] = {}
    graph_publication_report: dict[str, Any] = {
        "counts": {
            "workflows_targeted": 0,
            "workflows_published": 0,
            "workflows_skipped_missing_registration": 0,
            "workflows_skipped_missing_concept": 0,
            "step_concepts_created": 0,
            "action_concepts_created": 0,
            "validation_failures": 0,
            "errors": 0,
        },
        "published_workflow_ids": [],
        "skipped_missing_registration_workflow_ids": [],
        "skipped_missing_concept_workflow_ids": [],
        "created_step_concept_ids": [],
        "created_action_concept_ids": [],
        "validation_failures_by_workflow_id": {},
        "errors_by_workflow_id": {},
    }

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
                parent_ids = [preferred_type_id] if preferred_type_id else []
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

    if publish_canonical_graphs:
        try:
            graph_publication_report = publish_canonical_chat_workflow_graphs(
                registry=registry,
                create_missing=create_missing,
                target_workflow_ids=target_workflow_ids,
            )
        except Exception as exc:  # pragma: no cover - defensive
            publication_target_count = (
                len(
                    [
                        str(item).strip()
                        for item in (target_workflow_ids or ())
                        if isinstance(item, str) and str(item).strip()
                    ]
                )
                if target_workflow_ids is not None
                else len(CANONICAL_CHAT_WORKFLOW_IDS)
            )
            graph_publication_report = {
                "counts": {
                    "workflows_targeted": publication_target_count,
                    "workflows_published": 0,
                    "workflows_skipped_missing_registration": 0,
                    "workflows_skipped_missing_concept": 0,
                    "step_concepts_created": 0,
                    "action_concepts_created": 0,
                    "validation_failures": 0,
                    "errors": 1,
                },
                "published_workflow_ids": [],
                "skipped_missing_registration_workflow_ids": [],
                "skipped_missing_concept_workflow_ids": [],
                "created_step_concept_ids": [],
                "created_action_concept_ids": [],
                "validation_failures_by_workflow_id": {},
                "errors_by_workflow_id": {"__publication__": str(exc)},
            }

    return {
        "counts": {
            "registry_workflows": len(workflow_ids),
            "created": len(created),
            "updated": len(updated),
            "unchanged": len(unchanged),
            "errors": len(errors),
        },
        "required_type_ids": required_type_ids,
        "preferred_type_id": preferred_type_id,
        "created_workflow_ids": created,
        "updated_workflow_ids": updated,
        "unchanged_workflow_ids": unchanged,
        "errors_by_workflow_id": errors,
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

