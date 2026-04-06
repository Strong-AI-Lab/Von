"""Shared contracts for workflow-discovery gap recovery workflows.

These constants keep the runtime workflow definitions and the authoritative
Vontology publication path aligned around reusable action IDs, prompt IDs, and
context-mapping concept IDs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .subworkflow_contracts import WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE

WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID = (
    "#V#workflow_discovery_gap_recovery_workflow"
)
WORKFLOW_GAP_TEST_WORKFLOW_ID = "#V#workflow_gap_test_workflow"

WORKFLOW_GAP_COLLECT_CONTEXT_ACTION_ID = "workflow_gap.collect_context"
WORKFLOW_GAP_ANALYSE_RECOVERY_ACTION_ID = "workflow_gap.analyse_recovery"
WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID = "workflow_gap.prepare_candidate_spec"
WORKFLOW_GAP_DECIDE_TEST_ACTION_ID = "workflow_gap.decide_test"
WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID = "workflow_gap.finalise_recovery"
WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID = "workflow_gap.execute_candidate"
WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID = "workflow_gap.run_candidate_test"
WORKFLOW_GAP_MAX_REPAIR_ATTEMPTS = 3

WORKFLOW_GAP_ANALYSIS_PROMPT_TYPE_ID = "#V#prompt_for_llm"
WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID = "#V#workflow_gap_analysis_prompt"
WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID = "#V#workflow_gap_candidate_execution_prompt"
WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID = "#V#workflow_gap_test_prompt"
WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE = "#V#has_workflow_gap_analysis_prompt"
WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE = "#V#has_workflow_gap_test_prompt"
WORKFLOW_GAP_CANDIDATE_PROMPT_LINK_PREDICATE = "#V#has_workflow_gap_candidate_prompt"

WORKFLOW_GAP_CREATION_FAILURE_MODE_BINDINGS: tuple[tuple[str, str], ...] = (
    ("failure_mode", WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE),
)
WORKFLOW_GAP_TEST_FAILURE_MODE_BINDINGS: tuple[tuple[str, str], ...] = (
    ("failure_mode", WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE),
)

# Input-mapping concept IDs follow the canonical loader pattern
# `#V#workflow_mapping_<context_key>_to_<tool_param>_parameter`.
WORKFLOW_GAP_CREATE_WORKFLOW_SPEC_MAPPING_ID = (
    "#V#workflow_mapping_candidate_workflow_spec_to_workflow_spec_parameter"
)
WORKFLOW_GAP_CREATE_TEST_INPUTS_MAPPING_ID = (
    "#V#workflow_mapping_candidate_workflow_creation_test_inputs_to_test_run_inputs_parameter"
)
WORKFLOW_GAP_TEST_WORKFLOW_ID_MAPPING_ID = (
    "#V#workflow_mapping_created_candidate_workflow_id_to_candidate_workflow_id_parameter"
)
WORKFLOW_GAP_TEST_PROMPT_MAPPING_ID = (
    "#V#workflow_mapping_workflow_gap_request_text_to_prompt_parameter"
)
WORKFLOW_GAP_TEST_RECENT_TURNS_MAPPING_ID = (
    "#V#workflow_mapping_workflow_gap_recent_turns_to_workflow_gap_recent_turns_parameter"
)
WORKFLOW_GAP_TEST_ACCEPTANCE_MAPPING_ID = (
    "#V#workflow_mapping_workflow_gap_acceptance_requirements_to_workflow_gap_acceptance_requirements_parameter"
)
WORKFLOW_GAP_TEST_BASE_RESPONSE_MAPPING_ID = (
    "#V#workflow_mapping_workflow_gap_base_response_text_to_workflow_gap_base_response_text_parameter"
)
WORKFLOW_GAP_TEST_USER_CONCEPT_MAPPING_ID = (
    "#V#workflow_mapping_user_concept_id_to_user_concept_id_parameter"
)
WORKFLOW_GAP_TEST_ORG_CONCEPT_MAPPING_ID = (
    "#V#workflow_mapping_org_concept_id_to_org_concept_id_parameter"
)
WORKFLOW_GAP_TEST_SESSION_ID_MAPPING_ID = (
    "#V#workflow_mapping_conversation_session_id_to_conversation_session_id_parameter"
)
WORKFLOW_GAP_TEST_TURN_ID_MAPPING_ID = (
    "#V#workflow_mapping_turn_id_to_turn_id_parameter"
)


@dataclass(frozen=True)
class WorkflowGapOutputMappingSpec:
    concept_id: str
    tool_output_field: str
    context_key: str
    child_output_field: str | None = None


WORKFLOW_GAP_CREATE_TOOL_OUTPUT_MAPPINGS: tuple[WorkflowGapOutputMappingSpec, ...] = (
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_result_workflow_concept_id_"
            "to_created_candidate_workflow_id"
        ),
        tool_output_field="result.workflow_concept_id",
        context_key="created_candidate_workflow_id",
        child_output_field="workflow_concept_id",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_result_structural_validation_passed_"
            "to_candidate_creation_structural_validation_passed"
        ),
        tool_output_field="result.structural_validation_passed",
        context_key="candidate_creation_structural_validation_passed",
        child_output_field="structural_validation_passed",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_result_postconditions_verified_"
            "to_candidate_creation_postconditions_verified"
        ),
        tool_output_field="result.postconditions_verified",
        context_key="candidate_creation_postconditions_verified",
        child_output_field="postconditions_verified",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_result_workflow_discoverable_"
            "to_candidate_creation_workflow_discoverable"
        ),
        tool_output_field="result.workflow_discoverable",
        context_key="candidate_creation_workflow_discoverable",
        child_output_field="workflow_discoverable",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_child_workflow_failed_"
            "to_candidate_creation_child_failed"
        ),
        tool_output_field="child_workflow_failed",
        context_key="candidate_creation_child_failed",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_subworkflow_error_"
            "to_candidate_creation_error"
        ),
        tool_output_field="subworkflow_error",
        context_key="candidate_creation_error",
    ),
)

WORKFLOW_GAP_TEST_TOOL_OUTPUT_MAPPINGS: tuple[WorkflowGapOutputMappingSpec, ...] = (
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_result_workflow_gap_test_passed_"
            "to_workflow_gap_test_passed"
        ),
        tool_output_field="result.workflow_gap_test_passed",
        context_key="workflow_gap_test_passed",
        child_output_field="workflow_gap_test_passed",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_result_workflow_gap_test_response_text_"
            "to_workflow_gap_test_response_text"
        ),
        tool_output_field="result.workflow_gap_test_response_text",
        context_key="workflow_gap_test_response_text",
        child_output_field="workflow_gap_test_response_text",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_result_workflow_gap_test_tool_invocations_"
            "to_workflow_gap_test_tool_invocations"
        ),
        tool_output_field="result.workflow_gap_test_tool_invocations",
        context_key="workflow_gap_test_tool_invocations",
        child_output_field="workflow_gap_test_tool_invocations",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_result_workflow_gap_test_tool_messages_"
            "to_workflow_gap_test_tool_messages"
        ),
        tool_output_field="result.workflow_gap_test_tool_messages",
        context_key="workflow_gap_test_tool_messages",
        child_output_field="workflow_gap_test_tool_messages",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_result_workflow_gap_test_report_"
            "to_workflow_gap_test_report"
        ),
        tool_output_field="result.workflow_gap_test_report",
        context_key="workflow_gap_test_report",
        child_output_field="workflow_gap_test_report",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_child_workflow_failed_"
            "to_workflow_gap_test_child_failed"
        ),
        tool_output_field="child_workflow_failed",
        context_key="workflow_gap_test_child_failed",
    ),
    WorkflowGapOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_subworkflow_error_"
            "to_workflow_gap_test_error"
        ),
        tool_output_field="subworkflow_error",
        context_key="workflow_gap_test_error",
    ),
)

WORKFLOW_GAP_CREATE_WRITES_CONTEXT_KEYS: tuple[str, ...] = (
    "created_candidate_workflow_id",
    "candidate_creation_structural_validation_passed",
    "candidate_creation_postconditions_verified",
    "candidate_creation_workflow_discoverable",
    "candidate_creation_child_failed",
    "candidate_creation_error",
)

WORKFLOW_GAP_TEST_WRITES_CONTEXT_KEYS: tuple[str, ...] = (
    "workflow_gap_test_passed",
    "workflow_gap_test_response_text",
    "workflow_gap_test_tool_invocations",
    "workflow_gap_test_tool_messages",
    "workflow_gap_test_report",
    "workflow_gap_test_child_failed",
    "workflow_gap_test_error",
)

DEFAULT_WORKFLOW_GAP_PROMPT_WORKFLOW_IDS: tuple[str, ...] = (
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
)

__all__ = [
    "DEFAULT_WORKFLOW_GAP_PROMPT_WORKFLOW_IDS",
    "WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID",
    "WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID",
    "WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE",
    "WORKFLOW_GAP_ANALYSIS_PROMPT_TYPE_ID",
    "WORKFLOW_GAP_COLLECT_CONTEXT_ACTION_ID",
    "WORKFLOW_GAP_CREATE_TEST_INPUTS_MAPPING_ID",
    "WORKFLOW_GAP_CREATE_TOOL_OUTPUT_MAPPINGS",
    "WORKFLOW_GAP_CREATE_WORKFLOW_SPEC_MAPPING_ID",
    "WORKFLOW_GAP_CREATE_WRITES_CONTEXT_KEYS",
    "WORKFLOW_GAP_CREATION_FAILURE_MODE_BINDINGS",
    "WORKFLOW_GAP_DECIDE_TEST_ACTION_ID",
    "WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID",
    "WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID",
    "WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID",
    "WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID",
    "WORKFLOW_GAP_TEST_ACCEPTANCE_MAPPING_ID",
    "WORKFLOW_GAP_TEST_BASE_RESPONSE_MAPPING_ID",
    "WORKFLOW_GAP_TEST_FAILURE_MODE_BINDINGS",
    "WORKFLOW_GAP_TEST_ORG_CONCEPT_MAPPING_ID",
    "WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID",
    "WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE",
    "WORKFLOW_GAP_TEST_PROMPT_MAPPING_ID",
    "WORKFLOW_GAP_TEST_RECENT_TURNS_MAPPING_ID",
    "WORKFLOW_GAP_TEST_SESSION_ID_MAPPING_ID",
    "WORKFLOW_GAP_TEST_TOOL_OUTPUT_MAPPINGS",
    "WORKFLOW_GAP_TEST_TURN_ID_MAPPING_ID",
    "WORKFLOW_GAP_TEST_USER_CONCEPT_MAPPING_ID",
    "WORKFLOW_GAP_TEST_WORKFLOW_ID",
    "WORKFLOW_GAP_TEST_WORKFLOW_ID_MAPPING_ID",
    "WORKFLOW_GAP_TEST_WRITES_CONTEXT_KEYS",
    "WorkflowGapOutputMappingSpec",
]
