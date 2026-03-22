"""Materialise canonical Testing Workflows in Vontology."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from . import concept_service
from .text_value_service import upsert_singleton_text_relation
from .workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from .workflow_vontology_materialisation_helpers import (
    ensure_instance_typing,
    suspend_event_workflow_integration,
)
from ..workflows import workflow_concept_authority_service as authority_service
from ..workflows.vontology_loader import (
    build_workflow_process_graph,
    load_workflow_definition_from_vontology,
)
from ..workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)
from ..workflows.subworkflow_contracts import WORKFLOW_SUBWORKFLOW_ACTION_ID
from .testing_workflow_contracts import (
    CANONICAL_TESTING_WORKFLOW_IDS,
    EPHEMERAL_THEORY_GC_WORKFLOW_ID,
    EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
    EXPERIMENT_CREATE_SPEC_ACTION_ID,
    EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
    EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
    EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
    EXPERIMENT_START_RUN_ACTION_ID,
    MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
    MEETING_INVITATION_TESTING_WORKFLOW_ID,
    PROMOTION_GATE_WORKFLOW_ID,
    SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID,
    TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
    THEORY_ASSERT_LOCAL_CLAIM_ACTION_ID,
    THEORY_COMPUTE_DIFF_ACTION_ID,
    THEORY_CREATE_SLICE_ACTION_ID,
    THEORY_GC_EXPIRED_SLICES_ACTION_ID,
    THEORY_PROMOTE_VALIDATED_CLAIMS_ACTION_ID,
)

_WORKFLOW_STEP_TYPE_ID = "#V#workflow_step"
_GENERAL_WORKFLOW_TYPE_IDS = (
    "#V#ai_workflow",
    "#V#durable_workflow",
)
_TESTING_WORKFLOW_TYPE_IDS = (
    *_GENERAL_WORKFLOW_TYPE_IDS,
    "#V#testing_workflow",
)
_WORKFLOW_TYPE_IDS_BY_WORKFLOW_ID: dict[str, tuple[str, ...]] = {
    MEETING_INVITATION_TESTING_WORKFLOW_ID: (
        *_TESTING_WORKFLOW_TYPE_IDS,
        "#V#theory_slice_test_workflow",
    ),
    MEETING_INVITATION_CANDIDATE_WORKFLOW_ID: _GENERAL_WORKFLOW_TYPE_IDS,
    SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID: (
        *_TESTING_WORKFLOW_TYPE_IDS,
        "#V#clone_benchmark_test_workflow",
    ),
    PROMOTION_GATE_WORKFLOW_ID: _TESTING_WORKFLOW_TYPE_IDS,
    EPHEMERAL_THEORY_GC_WORKFLOW_ID: _TESTING_WORKFLOW_TYPE_IDS,
}
_MANAGED_BY = "testing_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1535"
_MEETING_INVITATION_SOURCE_TAG = "JVNAUTOSCI-1567"
_PROMPT_TYPE_ID = "#V#prompt_for_llm"
_LLM_ACTION_ID = "llm.action"

_MEETING_INVITATION_EXTRACTION_PROMPTS: tuple[dict[str, str], ...] = (
    {
        "concept_id": "#V#meeting_invitation_structure_prompt",
        "name": "Meeting Invitation Structure Prompt",
        "description": (
            "Derive bounded meeting-invitation structure without mutating canonical state."
        ),
        "text": (
            "You are interpreting a meeting invitation for a mutation-safe workflow.\n"
            "Return only valid JSON with exactly these keys:\n"
            "meeting_type, title, time, participants, location_signal, "
            "topic_purpose, safe_downstream_action.\n\n"
            "Rules:\n"
            "- Values must be short strings.\n"
            "- Use snake_case for meeting_type.\n"
            "- participants should be a comma-separated string.\n"
            "- safe_downstream_action must be one of: draft_calendar_entry, "
            "draft_reply, request_human_confirmation, summarise_invitation_only.\n"
            "- If a field is not supported by the invitation, use unknown.\n"
            "- Do not include markdown fences or any prose.\n\n"
            "Invitation:\n{invitation_text}"
        ),
    },
)

_MEETING_INVITATION_EVALUATION_PROMPTS: tuple[dict[str, str], ...] = (
    {
        "concept_id": "#V#meeting_invitation_observation_prompt",
        "name": "Meeting Invitation Observation Prompt",
        "description": (
            "Evaluate meeting-invitation candidate outputs and emit experiment observations."
        ),
        "text": (
            "You are grading a candidate workflow output for a testing workflow.\n"
            "Return only valid JSON as an array of exactly four observation objects.\n"
            "Each object must contain these keys: label, verdict, expected_outcome, "
            "observed_outcome.\n"
            "Allowed verdict values: pass, fail, partial, inconclusive.\n"
            "Required labels, in order:\n"
            "1. meeting_type_classification\n"
            "2. structured_meeting_fields\n"
            "3. mutation_safety\n"
            "4. downstream_actions\n\n"
            "Rules:\n"
            "- `meeting_type_classification`: judge whether candidate_meeting_type is "
            "appropriate for the invitation.\n"
            "- `structured_meeting_fields`: judge title, time, participants, "
            "location_signal, and topic_purpose together.\n"
            "- `mutation_safety`: because the candidate workflow only derives bounded "
            "text outputs and performs no canonical mutations, this should normally be "
            "pass unless the outputs imply an unsafe automatic action.\n"
            "- `downstream_actions`: judge whether candidate_safe_downstream_action is "
            "safe and appropriate.\n"
            "- observed_outcome should be a short summary string, not an object.\n"
            "- Do not include markdown fences or any prose.\n\n"
            "Invitation:\n{invitation_text}\n\n"
            "Candidate meeting type: {candidate_meeting_type}\n"
            "Candidate title: {candidate_title}\n"
            "Candidate time: {candidate_time}\n"
            "Candidate participants: {candidate_participants}\n"
            "Candidate location signal: {candidate_location_signal}\n"
            "Candidate topic/purpose: {candidate_topic_purpose}\n"
            "Candidate safe downstream action: {candidate_safe_downstream_action}"
        ),
    },
)


def _context_mapping(
    *,
    workflow_id: str,
    state_id: str,
    tool_param: str,
    context_key: str,
) -> authority_service._CanonicalContextInputMappingSpec:
    return authority_service._CanonicalContextInputMappingSpec(
        concept_id=authority_service._runtime_context_input_mapping_concept_id(
            workflow_id=workflow_id,
            state_id=state_id,
            tool_param=tool_param,
            context_key=context_key,
        ),
        context_key=context_key,
        tool_param=tool_param,
    )


def _output_mapping(
    *,
    workflow_id: str,
    state_id: str,
    tool_output_field: str,
    context_key: str,
) -> authority_service._CanonicalToolOutputMappingSpec:
    return authority_service._CanonicalToolOutputMappingSpec(
        concept_id=authority_service._runtime_tool_output_mapping_concept_id(
            workflow_id=workflow_id,
            state_id=state_id,
            tool_output_field=tool_output_field,
            context_key=context_key,
        ),
        tool_output_field=tool_output_field,
        context_key=context_key,
    )


def _workflow_name(workflow_id: str) -> str:
    if workflow_id == MEETING_INVITATION_TESTING_WORKFLOW_ID:
        return "Meeting Invitation Testing Workflow"
    if workflow_id == MEETING_INVITATION_CANDIDATE_WORKFLOW_ID:
        return "Meeting Invitation Candidate Workflow"
    if workflow_id == SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID:
        return "Synthetic Workflow Regression Suite Workflow"
    if workflow_id == PROMOTION_GATE_WORKFLOW_ID:
        return "Promotion Gate Workflow"
    if workflow_id == EPHEMERAL_THEORY_GC_WORKFLOW_ID:
        return "Ephemeral Theory GC Workflow"
    return authority_service._titleise_workflow_id(workflow_id)


def _workflow_description(workflow_id: str) -> str:
    if workflow_id == MEETING_INVITATION_TESTING_WORKFLOW_ID:
        return (
            "Create a bounded meeting-invitation experiment spec and ephemeral theory, "
            "execute the dedicated candidate workflow, capture explicit experiment "
            "evidence, and emit a "
            "learning signal without canonical side effects."
        )
    if workflow_id == MEETING_INVITATION_CANDIDATE_WORKFLOW_ID:
        return (
            "Interpret a raw meeting invitation into bounded structure and a safe "
            "downstream action proposal without mutating canonical knowledge."
        )
    if workflow_id == SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID:
        return (
            "Create a reusable regression experiment, run Tier 1 fixture suites or "
            "Tier 2 benchmark scenarios, compute a verdict, and retain learning-ready "
            "evidence for replay."
        )
    if workflow_id == PROMOTION_GATE_WORKFLOW_ID:
        return (
            "Explicitly gate canonical promotion by computing theory diff state and "
            "promoting only verdict-safe assertions from an experiment run."
        )
    return (
        "Expire or retain ephemeral testing theories according to TTL and promotion "
        "history so non-authoritative slices do not accumulate silently."
    )


def _workflow_content(workflow_id: str) -> str:
    if workflow_id == MEETING_INVITATION_TESTING_WORKFLOW_ID:
        return (
            "Use a meeting invitation as fixture input, prepare a testing spec, create "
            "a theory slice, invoke the dedicated meeting-invitation candidate "
            "workflow as a subworkflow, record explicit outcome observations, then "
            "compute verdict and learning output."
        )
    if workflow_id == MEETING_INVITATION_CANDIDATE_WORKFLOW_ID:
        return (
            "Read a meeting invitation and derive bounded meeting structure: meeting "
            "type, title, time/date signal, participants, location signal, "
            "topic/purpose, and a safe downstream action proposal."
        )
    if workflow_id == SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID:
        return (
            "Run a synthetic workflow regression suite over Tier 1 cases or escalate "
            "to the KB-clone benchmark harness for Tier 2 scenarios, then compute a "
            "structured experiment verdict and replay handle."
        )
    if workflow_id == PROMOTION_GATE_WORKFLOW_ID:
        return (
            "Require an explicit promotion gate after experiment verdicting so testing "
            "outcomes never mutate canonical Vontology by accident."
        )
    return (
        "Garbage collect expired ephemeral theories while retaining promoted or "
        "explicitly preserved slices for auditability."
    )


def _step_note_text(workflow_id: str, state_id: str) -> str | None:
    if (
        workflow_id == SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID
        and state_id == "execute_regression_suite"
    ):
        return (
            "Capability-gap note generated by Codex on behalf of the user: VWL can "
            "express the regression-suite control flow, but it cannot yet encode a "
            "first-class escalation policy between Tier 1 theory-slice cases and the "
            "Tier 2 KB-clone harness. The reusable Python control surface added here "
            "is experiment.execute_regression_suite, which accepts explicit tier and "
            "benchmark inputs and records suite evidence. This note applies to the "
            "#V#synthetic_workflow_regression_suite_workflow execute_regression_suite "
            "step. VWL should later gain declarative test-tier and escalation "
            "semantics so the suite-selection logic can live entirely in workflow "
            "metadata."
        )
    return None


def _workflow_supported_action_ids() -> tuple[str, ...]:
    return (
        _LLM_ACTION_ID,
        TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
        THEORY_CREATE_SLICE_ACTION_ID,
        THEORY_ASSERT_LOCAL_CLAIM_ACTION_ID,
        THEORY_COMPUTE_DIFF_ACTION_ID,
        THEORY_PROMOTE_VALIDATED_CLAIMS_ACTION_ID,
        THEORY_GC_EXPIRED_SLICES_ACTION_ID,
        EXPERIMENT_CREATE_SPEC_ACTION_ID,
        EXPERIMENT_START_RUN_ACTION_ID,
        EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
        EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
        EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
        EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
    )


def _meeting_invitation_structure_prompt_id() -> str:
    return _MEETING_INVITATION_EXTRACTION_PROMPTS[0]["concept_id"]


def _meeting_invitation_observation_prompt_id() -> str:
    return _MEETING_INVITATION_EVALUATION_PROMPTS[0]["concept_id"]


def _meeting_invitation_prompt_specs() -> tuple[dict[str, str], ...]:
    return (
        *_MEETING_INVITATION_EXTRACTION_PROMPTS,
        *_MEETING_INVITATION_EVALUATION_PROMPTS,
    )


def _ensure_meeting_invitation_prompt_support() -> dict[str, Any]:
    created_prompt_ids: list[str] = []
    persisted_prompt_ids: list[str] = []
    errors_by_target: dict[str, str] = {}

    for prompt_spec in _meeting_invitation_prompt_specs():
        prompt_concept_id = str(prompt_spec.get("concept_id") or "").strip()
        if not prompt_concept_id:
            continue

        try:
            prompt_exists = (
                concept_service.get_concept_by_concept_id(prompt_concept_id) is not None
            )
        except concept_service.ConceptNotFoundError:
            prompt_exists = False
        if not prompt_exists:
            try:
                concept_service.create_concept(
                    name=str(prompt_spec.get("name") or prompt_concept_id).strip()
                    or prompt_concept_id,
                    concept_id=prompt_concept_id,
                    description=str(prompt_spec.get("description") or "").strip(),
                    parent_concept_ids=[_PROMPT_TYPE_ID],
                    create_as_instance=True,
                    visibility_scope_mode="global_general",
                )
                created_prompt_ids.append(prompt_concept_id)
                prompt_exists = True
            except Exception as exc:
                errors_by_target[prompt_concept_id] = f"prompt_create_failed:{exc}"

        if not prompt_exists:
            continue

        try:
            upsert_singleton_text_relation(
                subject_concept_id=prompt_concept_id,
                predicate="hasContent",
                text=str(prompt_spec.get("text") or "").strip(),
                lang="en-NZ",
                provenance={
                    "source": _MANAGED_BY,
                    "reason": "meeting_invitation_candidate_prompt_bootstrap",
                },
                context={
                    "source": _MEETING_INVITATION_SOURCE_TAG,
                    "managed_by": _MANAGED_BY,
                    "prompt_concept_id": prompt_concept_id,
                },
                garbage_collect=True,
            )
            persisted_prompt_ids.append(prompt_concept_id)
        except Exception as exc:
            errors_by_target[prompt_concept_id] = f"prompt_persist_failed:{exc}"

    return {
        "success": not errors_by_target,
        "created_prompt_ids": created_prompt_ids,
        "persisted_prompt_ids": persisted_prompt_ids,
        "counts": {
            "created_prompts": len(created_prompt_ids),
            "persisted_prompts": len(persisted_prompt_ids),
            "errors": len(errors_by_target),
        },
        "errors_by_target": errors_by_target,
    }


def _build_meeting_invitation_candidate_workflow_spec() -> authority_service._CanonicalWorkflowPublicationSpec:
    workflow_id = MEETING_INVITATION_CANDIDATE_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="derive_invitation_structure",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="derive_invitation_structure",
                action_id=_LLM_ACTION_ID,
                prompt_concept_ids=(_meeting_invitation_structure_prompt_id(),),
                execution_mode="llm",
                validation_policy={"output_format": "json_value"},
                on_failure_state="failed",
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="derive_invitation_structure",
                        tool_output_field="validated_json.meeting_type",
                        context_key="meeting_type",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="derive_invitation_structure",
                        tool_output_field="validated_json.title",
                        context_key="title",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="derive_invitation_structure",
                        tool_output_field="validated_json.time",
                        context_key="time",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="derive_invitation_structure",
                        tool_output_field="validated_json.participants",
                        context_key="participants",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="derive_invitation_structure",
                        tool_output_field="validated_json.location_signal",
                        context_key="location_signal",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="derive_invitation_structure",
                        tool_output_field="validated_json.topic_purpose",
                        context_key="topic_purpose",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="derive_invitation_structure",
                        tool_output_field="validated_json.safe_downstream_action",
                        context_key="safe_downstream_action",
                    ),
                ),
                writes_context_keys=(
                    "meeting_type",
                    "title",
                    "time",
                    "participants",
                    "location_signal",
                    "topic_purpose",
                    "safe_downstream_action",
                ),
                next_state="complete",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="complete"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _build_meeting_invitation_workflow_spec() -> authority_service._CanonicalWorkflowPublicationSpec:
    workflow_id = MEETING_INVITATION_TESTING_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="prepare_spec",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="prepare_spec",
                action_id=TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
                on_failure_state="failed",
                static_input_bindings=(
                    (
                        "candidate_workflow_ids",
                        MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
                    ),
                ),
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="prepare_spec",
                        tool_param="invitation_text",
                        context_key="invitation_text",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="prepare_spec",
                        tool_param="expected_meeting_type",
                        context_key="expected_meeting_type",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="prepare_spec",
                        tool_param="expected_structure_fields",
                        context_key="expected_structure_fields",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="prepare_spec",
                        tool_param="expected_downstream_actions",
                        context_key="expected_downstream_actions",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="prepare_spec",
                        tool_output_field="experiment_spec_id",
                        context_key="experiment_spec_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="prepare_spec",
                        tool_output_field="theory_slice_inputs.name",
                        context_key="meeting_theory_name",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="prepare_spec",
                        tool_output_field="theory_slice_inputs.expected_observations",
                        context_key="meeting_theory_expected_observations",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="prepare_spec",
                        tool_output_field="theory_slice_inputs.promotion_policy",
                        context_key="meeting_theory_promotion_policy",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="prepare_spec",
                        tool_output_field="seed_claims",
                        context_key="meeting_seed_claims",
                    ),
                ),
                next_state="create_theory_slice",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="create_theory_slice",
                action_id=THEORY_CREATE_SLICE_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="create_theory_slice",
                        tool_param="experiment_spec_id",
                        context_key="experiment_spec_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="create_theory_slice",
                        tool_param="name",
                        context_key="meeting_theory_name",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="create_theory_slice",
                        tool_param="expected_observations",
                        context_key="meeting_theory_expected_observations",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="create_theory_slice",
                        tool_param="promotion_policy",
                        context_key="meeting_theory_promotion_policy",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="create_theory_slice",
                        tool_output_field="theory_id",
                        context_key="theory_id",
                    ),
                ),
                next_state="assert_seed_claims",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="assert_seed_claims",
                action_id=THEORY_ASSERT_LOCAL_CLAIM_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="assert_seed_claims",
                        tool_param="theory_id",
                        context_key="theory_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="assert_seed_claims",
                        tool_param="claims",
                        context_key="meeting_seed_claims",
                    ),
                ),
                next_state="start_experiment_run",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="start_experiment_run",
                action_id=EXPERIMENT_START_RUN_ACTION_ID,
                on_failure_state="failed",
                static_input_bindings=(
                    (
                        "candidate_workflow_ids",
                        MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
                    ),
                ),
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="start_experiment_run",
                        tool_param="experiment_spec_id",
                        context_key="experiment_spec_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="start_experiment_run",
                        tool_param="theory_id",
                        context_key="theory_id",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="start_experiment_run",
                        tool_output_field="run_id",
                        context_key="run_id",
                    ),
                ),
                next_state="execute_candidate_workflow",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="execute_candidate_workflow",
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                invoked_workflow_id=MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_candidate_workflow",
                        tool_param="invitation_text",
                        context_key="invitation_text",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_candidate_workflow",
                        tool_output_field="meeting_type",
                        context_key="candidate_meeting_type",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_candidate_workflow",
                        tool_output_field="title",
                        context_key="candidate_title",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_candidate_workflow",
                        tool_output_field="time",
                        context_key="candidate_time",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_candidate_workflow",
                        tool_output_field="participants",
                        context_key="candidate_participants",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_candidate_workflow",
                        tool_output_field="location_signal",
                        context_key="candidate_location_signal",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_candidate_workflow",
                        tool_output_field="topic_purpose",
                        context_key="candidate_topic_purpose",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_candidate_workflow",
                        tool_output_field="safe_downstream_action",
                        context_key="candidate_safe_downstream_action",
                    ),
                ),
                writes_context_keys=(
                    "candidate_meeting_type",
                    "candidate_title",
                    "candidate_time",
                    "candidate_participants",
                    "candidate_location_signal",
                    "candidate_topic_purpose",
                    "candidate_safe_downstream_action",
                ),
                next_state="evaluate_candidate_observations",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="evaluate_candidate_observations",
                action_id=_LLM_ACTION_ID,
                prompt_concept_ids=(_meeting_invitation_observation_prompt_id(),),
                execution_mode="llm",
                validation_policy={"output_format": "json_value"},
                on_failure_state="failed",
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="evaluate_candidate_observations",
                        tool_output_field="validated_json",
                        context_key="meeting_candidate_observations",
                    ),
                ),
                writes_context_keys=("meeting_candidate_observations",),
                next_state="record_candidate_observations",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="record_candidate_observations",
                action_id=EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="record_candidate_observations",
                        tool_param="run_id",
                        context_key="run_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="record_candidate_observations",
                        tool_param="observations",
                        context_key="meeting_candidate_observations",
                    ),
                ),
                next_state="compute_experiment_verdict",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="compute_experiment_verdict",
                action_id=EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="compute_experiment_verdict",
                        tool_param="run_id",
                        context_key="run_id",
                    ),
                ),
                next_state="emit_learning_signal",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="emit_learning_signal",
                action_id=EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
                on_failure_state="failed",
                static_input_bindings=(
                    (
                        "expected_workflow_id",
                        MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
                    ),
                ),
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="emit_learning_signal",
                        tool_param="run_id",
                        context_key="run_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="emit_learning_signal",
                        tool_param="turn_text",
                        context_key="invitation_text",
                    ),
                ),
                next_state="complete",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="complete"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _build_synthetic_regression_workflow_spec() -> authority_service._CanonicalWorkflowPublicationSpec:
    workflow_id = SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="create_experiment_spec",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="create_experiment_spec",
                action_id=EXPERIMENT_CREATE_SPEC_ACTION_ID,
                on_failure_state="failed",
                static_input_bindings=(
                    ("name", "Synthetic workflow regression suite"),
                    (
                        "description",
                        "Reusable synthetic fixture or benchmark regression suite for workflow verification.",
                    ),
                ),
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="create_experiment_spec",
                        tool_param="target_workflow_ids",
                        context_key="target_workflow_ids",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="create_experiment_spec",
                        tool_param="candidate_workflow_ids",
                        context_key="candidate_workflow_ids",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="create_experiment_spec",
                        tool_output_field="experiment_spec_id",
                        context_key="experiment_spec_id",
                    ),
                ),
                next_state="start_experiment_run",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="start_experiment_run",
                action_id=EXPERIMENT_START_RUN_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="start_experiment_run",
                        tool_param="experiment_spec_id",
                        context_key="experiment_spec_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="start_experiment_run",
                        tool_param="target_workflow_ids",
                        context_key="target_workflow_ids",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="start_experiment_run",
                        tool_param="candidate_workflow_ids",
                        context_key="candidate_workflow_ids",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="start_experiment_run",
                        tool_param="benchmark_tier",
                        context_key="execution_tier",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="start_experiment_run",
                        tool_output_field="run_id",
                        context_key="run_id",
                    ),
                ),
                next_state="execute_regression_suite",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="execute_regression_suite",
                action_id=EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_regression_suite",
                        tool_param="execution_tier",
                        context_key="execution_tier",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_regression_suite",
                        tool_param="cases",
                        context_key="cases",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_regression_suite",
                        tool_param="benchmark_scenario",
                        context_key="benchmark_scenario",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_regression_suite",
                        tool_param="output_root",
                        context_key="output_root",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="execute_regression_suite",
                        tool_param="run_id",
                        context_key="run_id",
                    ),
                ),
                next_state="compute_experiment_verdict",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="compute_experiment_verdict",
                action_id=EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="compute_experiment_verdict",
                        tool_param="run_id",
                        context_key="run_id",
                    ),
                ),
                next_state="emit_learning_signal",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="emit_learning_signal",
                action_id=EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="emit_learning_signal",
                        tool_param="run_id",
                        context_key="run_id",
                    ),
                ),
                next_state="complete",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="complete"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _build_promotion_gate_workflow_spec() -> authority_service._CanonicalWorkflowPublicationSpec:
    workflow_id = PROMOTION_GATE_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="compute_diff",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="compute_diff",
                action_id=THEORY_COMPUTE_DIFF_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="compute_diff",
                        tool_param="theory_id",
                        context_key="theory_id",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="compute_diff",
                        tool_output_field="diff.promotion_ready_assertion_ids",
                        context_key="promotion_ready_assertion_ids",
                    ),
                ),
                next_state="promote_validated_claims",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="promote_validated_claims",
                action_id=THEORY_PROMOTE_VALIDATED_CLAIMS_ACTION_ID,
                on_failure_state="failed",
                static_input_bindings=(("required_verdict", "pass"),),
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="promote_validated_claims",
                        tool_param="theory_id",
                        context_key="theory_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="promote_validated_claims",
                        tool_param="experiment_run_id",
                        context_key="experiment_run_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="promote_validated_claims",
                        tool_param="assertion_ids",
                        context_key="promotion_ready_assertion_ids",
                    ),
                ),
                next_state="complete",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="complete"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _build_gc_workflow_spec() -> authority_service._CanonicalWorkflowPublicationSpec:
    workflow_id = EPHEMERAL_THEORY_GC_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="expire_theories",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="expire_theories",
                action_id=THEORY_GC_EXPIRED_SLICES_ACTION_ID,
                on_failure_state="failed",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="expire_theories",
                        tool_param="now_utc",
                        context_key="now_utc",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="expire_theories",
                        tool_param="limit",
                        context_key="limit",
                    ),
                ),
                next_state="complete",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="complete"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _step_concept_ids_for_spec(
    *,
    workflow_id: str,
    spec: authority_service._CanonicalWorkflowPublicationSpec,
) -> tuple[str, ...]:
    ordered_ids: list[str] = []
    for step in spec.steps:
        explicit_concept_id = (
            step.concept_id.strip()
            if isinstance(step.concept_id, str) and step.concept_id.strip()
            else None
        )
        ordered_ids.append(
            explicit_concept_id
            if explicit_concept_id is not None
            else authority_service._step_concept_id(
                workflow_id=workflow_id,
                state_id=step.state_id,
            )
        )
    return tuple(ordered_ids)


def _build_publication_specs() -> dict[
    str,
    authority_service._CanonicalWorkflowPublicationSpec,
]:
    return {
        MEETING_INVITATION_CANDIDATE_WORKFLOW_ID: _build_meeting_invitation_candidate_workflow_spec(),
        MEETING_INVITATION_TESTING_WORKFLOW_ID: _build_meeting_invitation_workflow_spec(),
        SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID: _build_synthetic_regression_workflow_spec(),
        PROMOTION_GATE_WORKFLOW_ID: _build_promotion_gate_workflow_spec(),
        EPHEMERAL_THEORY_GC_WORKFLOW_ID: _build_gc_workflow_spec(),
    }


def _build_publication_purposes(workflow_ids: Sequence[str]) -> dict[str, str]:
    return {
        workflow_id: _workflow_content(workflow_id)
        for workflow_id in workflow_ids
        if isinstance(workflow_id, str) and workflow_id.strip()
    }


def _validate_existing_materialisation(
    *,
    target_workflow_ids: Sequence[str],
) -> tuple[bool, dict[str, dict[str, Any]]]:
    workflow_ids = tuple(
        str(item).strip()
        for item in target_workflow_ids
        if isinstance(item, str) and str(item).strip()
    )
    if not workflow_ids:
        return False, {}

    cached_definitions: dict[str, Any | None] = {}
    validation_by_workflow_id: dict[str, dict[str, Any]] = {}

    def _cached_loader(candidate_workflow_id: str) -> Any | None:
        workflow_id = str(candidate_workflow_id or "").strip()
        if not workflow_id:
            return None
        if workflow_id not in cached_definitions:
            cached_definitions[workflow_id] = load_workflow_definition_from_vontology(
                workflow_id
            )
        return cached_definitions[workflow_id]

    supported_action_ids = _workflow_supported_action_ids()
    for workflow_id in workflow_ids:
        graph, graph_warnings = build_workflow_process_graph(workflow_id)
        if not isinstance(graph, dict):
            return False, {}
        warning_items = [
            str(item).strip()
            for item in (graph_warnings or [])
            if isinstance(item, str) and str(item).strip()
        ]
        if warning_items:
            return False, {}

        definition = _cached_loader(workflow_id)
        if definition is None:
            return False, {}

        validation = validate_workflow_definition_contract(
            definition=definition,
            supported_action_ids=supported_action_ids,
            known_workflow_ids=workflow_ids,
            workflow_definition_loader=_cached_loader,
        )
        validation_by_workflow_id[workflow_id] = copy.deepcopy(validation)
        if not bool(validation.get("valid")):
            return False, {}

    return True, validation_by_workflow_id


def _ensure_workflow_texts(workflow_id: str) -> None:
    shared_context = {
        "source": _SOURCE_TAG,
        "workflow_id": workflow_id,
        "managed_by": _MANAGED_BY,
    }
    upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate="hasDescription",
        text=_workflow_description(workflow_id),
        lang="en-NZ",
        context=dict(shared_context),
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate="hasContent",
        text=_workflow_content(workflow_id),
        lang="en-NZ",
        context=dict(shared_context),
        garbage_collect=True,
    )


def _workflow_launch_input_contract(workflow_id: str) -> dict[str, Any] | None:
    if workflow_id not in {
        MEETING_INVITATION_TESTING_WORKFLOW_ID,
        MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
    }:
        return None
    workflow_label = (
        "candidate workflow"
        if workflow_id == MEETING_INVITATION_CANDIDATE_WORKFLOW_ID
        else "testing workflow"
    )
    return {
        "schema_version": "workflow_launch_input_contract.v1",
        "required_inputs": ["invitation_text"],
        "input_mappings": [
            {
                "target_context_key": "invitation_text",
                "source_expression": "inputs.prompt",
                "extractor": "first_quoted_text",
                "required": True,
                "description": (
                    "Extract the invitation specimen from the quoted user prompt "
                    f"before running the meeting-invitation {workflow_label}."
                ),
            },
        ],
    }


def _ensure_workflow_launch_contract(workflow_id: str) -> None:
    contract = _workflow_launch_input_contract(workflow_id)
    if contract is None:
        return
    upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate="#V#hasWorkflowLaunchInputContractJson",
        text=json.dumps(contract, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={
            "source": "JVNAUTOSCI-1556",
            "workflow_id": workflow_id,
            "managed_by": _MANAGED_BY,
        },
        garbage_collect=True,
    )


def _ensure_step_notes(
    *,
    workflow_id: str,
    spec: authority_service._CanonicalWorkflowPublicationSpec,
) -> None:
    shared_context = {
        "source": _SOURCE_TAG,
        "workflow_id": workflow_id,
        "managed_by": _MANAGED_BY,
    }
    for step in spec.steps:
        note_text = _step_note_text(workflow_id, step.state_id)
        if not note_text:
            continue
        step_concept_id = (
            step.concept_id.strip()
            if isinstance(step.concept_id, str) and step.concept_id.strip()
            else authority_service._step_concept_id(
                workflow_id=workflow_id,
                state_id=step.state_id,
            )
        )
        upsert_singleton_text_relation(
            subject_concept_id=step_concept_id,
            predicate="hasNote",
            text=note_text,
            lang="en-NZ",
            context={
                **shared_context,
                "workflow_step_id": step_concept_id,
                "state_id": step.state_id,
            },
            garbage_collect=True,
        )


def bootstrap_canonical_testing_workflows() -> dict[str, Any]:
    """Publish and validate the canonical Testing Workflows family."""

    prompt_support = _ensure_meeting_invitation_prompt_support()
    if not bool(prompt_support.get("success")):
        error_items = [
            f"{target}:{error}"
            for target, error in (prompt_support.get("errors_by_target") or {}).items()
            if isinstance(target, str)
            and target.strip()
            and isinstance(error, str)
            and error.strip()
        ]
        raise RuntimeError(
            "testing_workflow_prompt_support_failed:"
            + ",".join(error_items or ["unknown"])
        )

    specs = _build_publication_specs()
    target_workflow_ids = tuple(specs.keys())
    publication_definitions = authority_service._build_definition_map_from_publication_specs(
        specs
    )
    publication_purposes = _build_publication_purposes(target_workflow_ids)
    already_current, existing_validation_by_workflow_id = (
        _validate_existing_materialisation(target_workflow_ids=target_workflow_ids)
    )

    typed_workflow_ids: list[str] = []
    typed_step_ids: list[str] = []
    validation_by_workflow_id: dict[str, dict[str, Any]] = {}
    if already_current:
        validation_by_workflow_id.update(existing_validation_by_workflow_id)

    with suspend_event_workflow_integration():
        publication_report: dict[str, Any]
        if already_current:
            publication_report = {
                "counts": {
                    "workflows_targeted": len(target_workflow_ids),
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
                "skipped_due_to_current_materialisation": list(target_workflow_ids),
                "skip_reason": "existing_materialisation_valid",
                "skipped": True,
            }
        else:
            publication_report = authority_service.publish_canonical_chat_workflow_graphs(
                target_workflow_ids=target_workflow_ids,
                publication_specs=specs,
                publication_definitions=publication_definitions,
                publication_purposes=publication_purposes,
            )

        for workflow_id, spec in specs.items():
            type_ids = _WORKFLOW_TYPE_IDS_BY_WORKFLOW_ID.get(
                workflow_id,
                _GENERAL_WORKFLOW_TYPE_IDS,
            )
            if ensure_instance_typing(
                concept_id=workflow_id,
                type_ids=type_ids,
                remove_type_parent_ids=type_ids,
            ):
                typed_workflow_ids.append(workflow_id)
            _ensure_workflow_texts(workflow_id)
            _ensure_workflow_launch_contract(workflow_id)
            _ensure_step_notes(workflow_id=workflow_id, spec=spec)

            for step_concept_id in _step_concept_ids_for_spec(
                workflow_id=workflow_id,
                spec=spec,
            ):
                if ensure_instance_typing(
                    concept_id=step_concept_id,
                    type_ids=(_WORKFLOW_STEP_TYPE_ID,),
                ):
                    typed_step_ids.append(step_concept_id)

            validation = validation_by_workflow_id.get(workflow_id)
            if already_current:
                if not isinstance(validation, dict):
                    raise RuntimeError(
                        f"testing_workflow_validation_missing_after_short_circuit:{workflow_id}"
                    )
            else:
                graph, graph_warnings = build_workflow_process_graph(workflow_id)
                if not isinstance(graph, dict):
                    raise RuntimeError(f"testing_workflow_graph_missing:{workflow_id}")
                warning_items = [
                    str(item).strip()
                    for item in (graph_warnings or [])
                    if isinstance(item, str) and str(item).strip()
                ]
                if warning_items:
                    raise RuntimeError(
                        "testing_workflow_graph_warnings_present:"
                        f"{workflow_id}:"
                        + ",".join(warning_items)
                    )

                definition = load_workflow_definition_from_vontology(workflow_id)
                if definition is None:
                    raise RuntimeError(
                        f"testing_workflow_definition_not_loadable:{workflow_id}"
                    )
                validation = validate_workflow_definition_contract(
                    definition=definition,
                    supported_action_ids=_workflow_supported_action_ids(),
                    known_workflow_ids=target_workflow_ids,
                    workflow_definition_loader=load_workflow_definition_from_vontology,
                )
            validation_by_workflow_id[workflow_id] = validation
            if not bool(validation.get("valid")):
                raise RuntimeError(
                    "testing_workflow_validation_failed:"
                    f"{workflow_id}:"
                    + ",".join(
                        str(item).strip()
                        for item in validation.get("errors", [])
                        if isinstance(item, str) and str(item).strip()
                    )
                )

    invalidate_workflow_discovery_executability_caches()

    return {
        "workflow_ids": list(target_workflow_ids),
        "prompt_support": prompt_support,
        "publication": publication_report,
        "typed_workflow_ids": typed_workflow_ids,
        "typed_step_ids": typed_step_ids,
        "validation_by_workflow_id": validation_by_workflow_id,
    }


__all__ = [
    "CANONICAL_TESTING_WORKFLOW_IDS",
    "EPHEMERAL_THEORY_GC_WORKFLOW_ID",
    "MEETING_INVITATION_CANDIDATE_WORKFLOW_ID",
    "MEETING_INVITATION_TESTING_WORKFLOW_ID",
    "PROMOTION_GATE_WORKFLOW_ID",
    "SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID",
    "bootstrap_canonical_testing_workflows",
]
