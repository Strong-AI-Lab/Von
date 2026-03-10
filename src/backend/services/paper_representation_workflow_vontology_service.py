"""Materialise canonical scholarly-paper and arXiv workflows in Vontology."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from . import concept_service
from .text_value_service import upsert_singleton_text_relation
from .workflow_discovery_service import invalidate_workflow_discovery_executability_caches
from ..workflows import workflow_concept_authority_service as authority_service
from ..workflows.durable.paper_representation_workflow import (
    ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
    ARXIV_NORMALISE_SOURCE_ACTION_ID,
    SCHOLARLY_PAPER_ENRICH_ACTION_ID,
    SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
    SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
    SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
    SCHOLARLY_PAPER_VERIFY_ACTION_ID,
)
from ..workflows.durable.subworkflow_actions import WORKFLOW_SUBWORKFLOW_ACTION_ID
from ..workflows.vontology_loader import (
    build_workflow_process_graph,
    load_workflow_definition_from_vontology,
)
from ..workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)
from ..workflows.workflow_registry import WorkflowRegistration, WorkflowRegistry

SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#scholarly_paper_representation_workflow"
ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#arxiv_paper_representation_workflow"

_WORKFLOW_STEP_TYPE_ID = "#V#workflow_step"
_WORKFLOW_TYPE_IDS = ("#V#ai_workflow", "#V#durable_workflow")


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
    if workflow_id == SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID:
        return "Scholarly Paper Representation Workflow"
    if workflow_id == ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID:
        return "arXiv Paper Representation Workflow"
    return authority_service._titleise_workflow_id(workflow_id)


def _workflow_description(workflow_id: str) -> str:
    if workflow_id == SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID:
        return (
            "Canonical durable workflow for representing scholarly papers from file-copy "
            "artefacts, metadata, and verification requirements."
        )
    return (
        "Canonical arXiv wrapper workflow that normalises an arXiv source, acquires or "
        "finalises the paper artefact, delegates to the scholarly-paper workflow, and "
        "fails closed on incomplete representation."
    )


def _workflow_content(workflow_id: str) -> str:
    if workflow_id == SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID:
        return (
            "Represent a scholarly paper from a file copy by normalising inputs, "
            "materialising or reusing the paper concept, enriching metadata, resolving "
            "authors, and verifying the requested representation profile."
        )
    return (
        "Represent an arXiv paper by extracting a canonical arXiv identifier, deciding "
        "whether acquisition is required, invoking download_paper when needed, then "
        "delegating to the general scholarly-paper workflow with arXiv verification."
    )


def _workflow_supported_action_ids() -> tuple[str, ...]:
    return (
        SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
        SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
        SCHOLARLY_PAPER_ENRICH_ACTION_ID,
        SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
        SCHOLARLY_PAPER_VERIFY_ACTION_ID,
        ARXIV_NORMALISE_SOURCE_ACTION_ID,
        ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
        "download_paper",
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
    )


def _build_scholarly_workflow_spec() -> authority_service._CanonicalWorkflowPublicationSpec:
    workflow_id = SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="normalise_inputs",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="normalise_inputs",
                action_id=SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="paper_metadata",
                        context_key="paper_metadata",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="source_uri",
                        context_key="source_uri",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="verification_profile",
                        context_key="verification_profile",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="title",
                        context_key="title",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="summary",
                        context_key="summary",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="author_names",
                        context_key="author_names",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_param="topic_labels",
                        context_key="topic_labels",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="paper_metadata",
                        context_key="paper_metadata",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="source_uri",
                        context_key="source_uri",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="verification_profile",
                        context_key="verification_profile",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="title",
                        context_key="title",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="summary",
                        context_key="summary",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="author_names",
                        context_key="author_names",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="topic_labels",
                        context_key="topic_labels",
                    ),
                ),
                writes_context_keys=(
                    "file_copy_concept_id",
                    "paper_concept_id",
                    "paper_metadata",
                    "source_uri",
                    "arxiv_id",
                    "verification_profile",
                    "title",
                    "summary",
                    "author_names",
                    "topic_labels",
                ),
                on_failure_state="failed",
                next_state="materialise_base_representation",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="materialise_base_representation",
                action_id=SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_base_representation",
                        tool_param="file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_base_representation",
                        tool_param="paper_metadata",
                        context_key="paper_metadata",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_base_representation",
                        tool_param="arxiv_id",
                        context_key="arxiv_id",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_base_representation",
                        tool_output_field="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_base_representation",
                        tool_output_field="file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_base_representation",
                        tool_output_field="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_base_representation",
                        tool_output_field="scholarly_representation",
                        context_key="scholarly_representation",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_base_representation",
                        tool_output_field="representation_mode",
                        context_key="representation_mode",
                    ),
                ),
                writes_context_keys=(
                    "paper_concept_id",
                    "file_copy_concept_id",
                    "arxiv_id",
                    "scholarly_representation",
                    "representation_mode",
                ),
                on_failure_state="failed",
                next_state="enrich_from_metadata",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="enrich_from_metadata",
                action_id=SCHOLARLY_PAPER_ENRICH_ACTION_ID,
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_param="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_param="title",
                        context_key="title",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_param="summary",
                        context_key="summary",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_param="topic_labels",
                        context_key="topic_labels",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_param="source_uri",
                        context_key="source_uri",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_param="arxiv_id",
                        context_key="arxiv_id",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_output_field="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_output_field="title",
                        context_key="title",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_output_field="summary",
                        context_key="summary",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="enrich_from_metadata",
                        tool_output_field="topic_labels",
                        context_key="topic_labels",
                    ),
                ),
                writes_context_keys=("paper_concept_id", "title", "summary", "topic_labels"),
                on_failure_state="failed",
                next_state="resolve_authors",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="resolve_authors",
                action_id=SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="resolve_authors",
                        tool_param="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="resolve_authors",
                        tool_param="author_names",
                        context_key="author_names",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="resolve_authors",
                        tool_output_field="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="resolve_authors",
                        tool_output_field="author_names",
                        context_key="author_names",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="resolve_authors",
                        tool_output_field="author_concept_ids",
                        context_key="author_concept_ids",
                    ),
                ),
                writes_context_keys=("paper_concept_id", "author_names", "author_concept_ids"),
                on_failure_state="failed",
                next_state="verify_representation",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="verify_representation",
                action_id=SCHOLARLY_PAPER_VERIFY_ACTION_ID,
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="verification_profile",
                        context_key="verification_profile",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="topic_labels",
                        context_key="topic_labels",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_output_field="scholarly_representation_verified",
                        context_key="scholarly_representation_verified",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_output_field="verification_failures",
                        context_key="verification_failures",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_output_field="author_concept_ids",
                        context_key="author_concept_ids",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_output_field="topic_concept_ids",
                        context_key="topic_concept_ids",
                    ),
                ),
                writes_context_keys=(
                    "scholarly_representation_verified",
                    "verification_failures",
                    "author_concept_ids",
                    "topic_concept_ids",
                ),
                on_failure_state="failed",
                on_true_state="completed",
                on_false_state="failed",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="completed"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _build_arxiv_workflow_spec() -> authority_service._CanonicalWorkflowPublicationSpec:
    workflow_id = ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="normalise_arxiv_source",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="normalise_arxiv_source",
                action_id=ARXIV_NORMALISE_SOURCE_ACTION_ID,
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_arxiv_source",
                        tool_param="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_arxiv_source",
                        tool_param="source_uri",
                        context_key="source_uri",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_arxiv_source",
                        tool_param="prompt",
                        context_key="prompt",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_arxiv_source",
                        tool_param="original_filename",
                        context_key="original_filename",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_arxiv_source",
                        tool_output_field="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_arxiv_source",
                        tool_output_field="source_uri",
                        context_key="source_uri",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_arxiv_source",
                        tool_output_field="verification_profile",
                        context_key="verification_profile",
                    ),
                ),
                on_failure_state="failed",
                next_state="decide_acquisition_mode",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="decide_acquisition_mode",
                action_id=ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="decide_acquisition_mode",
                        tool_param="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="decide_acquisition_mode",
                        tool_param="file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="decide_acquisition_mode",
                        tool_output_field="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="decide_acquisition_mode",
                        tool_output_field="acquisition_required",
                        context_key="acquisition_required",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="decide_acquisition_mode",
                        tool_output_field="file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="decide_acquisition_mode",
                        tool_output_field="acquisition_mode",
                        context_key="acquisition_mode",
                    ),
                ),
                on_failure_state="failed",
                on_true_state="download_or_finalise",
                on_false_state="delegate_to_general_paper_workflow",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="download_or_finalise",
                action_id="download_paper",
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="download_or_finalise",
                        tool_param="arxiv_id",
                        context_key="arxiv_id",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="download_or_finalise",
                        tool_output_field="result.computer_file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="download_or_finalise",
                        tool_output_field="result.paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="download_or_finalise",
                        tool_output_field="result.scholarly_representation",
                        context_key="scholarly_representation",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="download_or_finalise",
                        tool_output_field="result.arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="download_or_finalise",
                        tool_output_field="result.scholarly_representation.title",
                        context_key="title",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="download_or_finalise",
                        tool_output_field="result.scholarly_representation.summary",
                        context_key="summary",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="download_or_finalise",
                        tool_output_field="result.scholarly_representation.author_names",
                        context_key="author_names",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="download_or_finalise",
                        tool_output_field="result.scholarly_representation.topic_labels",
                        context_key="topic_labels",
                    ),
                ),
                on_failure_state="failed",
                next_state="delegate_to_general_paper_workflow",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="delegate_to_general_paper_workflow",
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                invoked_workflow_id=SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
                static_input_bindings=(("verification_profile", "arxiv"),),
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="paper_metadata",
                        context_key="paper_metadata",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="source_uri",
                        context_key="source_uri",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="title",
                        context_key="title",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="summary",
                        context_key="summary",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="author_names",
                        context_key="author_names",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="topic_labels",
                        context_key="topic_labels",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_param="verification_profile",
                        context_key="verification_profile",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_output_field="result.paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_output_field="result.file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_output_field="result.scholarly_representation",
                        context_key="scholarly_representation",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_output_field="result.title",
                        context_key="title",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_output_field="result.summary",
                        context_key="summary",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_output_field="result.author_names",
                        context_key="author_names",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_output_field="result.topic_labels",
                        context_key="topic_labels",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_output_field="result.scholarly_representation_verified",
                        context_key="scholarly_representation_verified",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="delegate_to_general_paper_workflow",
                        tool_output_field="result.verification_failures",
                        context_key="verification_failures",
                    ),
                ),
                on_failure_state="failed",
                next_state="verify_arxiv_path",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="verify_arxiv_path",
                action_id=SCHOLARLY_PAPER_VERIFY_ACTION_ID,
                static_input_bindings=(("verification_profile", "arxiv"),),
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_arxiv_path",
                        tool_param="paper_concept_id",
                        context_key="paper_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_arxiv_path",
                        tool_param="file_copy_concept_id",
                        context_key="file_copy_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_arxiv_path",
                        tool_param="arxiv_id",
                        context_key="arxiv_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_arxiv_path",
                        tool_param="topic_labels",
                        context_key="topic_labels",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_arxiv_path",
                        tool_output_field="scholarly_representation_verified",
                        context_key="scholarly_representation_verified",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_arxiv_path",
                        tool_output_field="verification_failures",
                        context_key="verification_failures",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_arxiv_path",
                        tool_output_field="author_concept_ids",
                        context_key="author_concept_ids",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_arxiv_path",
                        tool_output_field="topic_concept_ids",
                        context_key="topic_concept_ids",
                    ),
                ),
                on_failure_state="failed",
                on_true_state="completed",
                on_false_state="failed",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="completed"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _load_concept(concept_id: str) -> dict[str, Any] | None:
    concept_id_clean = str(concept_id or "").strip()
    if not concept_id_clean:
        return None
    try:
        concept_doc = ConceptsRepository.find_one({"concept_id": concept_id_clean})
    except Exception:
        return None
    return dict(concept_doc) if isinstance(concept_doc, Mapping) else None


def _ensure_instance_typing(*, concept_id: str, type_ids: Sequence[str]) -> bool:
    concept_doc = _load_concept(concept_id)
    if concept_doc is None:
        return False

    relationships = dict(concept_doc.get("relationships") or {})
    existing = relationships.get("is_an_instance_of")
    if isinstance(existing, list):
        updated = [
            str(item).strip()
            for item in existing
            if isinstance(item, str) and str(item).strip()
        ]
    elif isinstance(existing, str) and existing.strip():
        updated = [existing.strip()]
    else:
        updated = []

    changed = False
    for type_id in type_ids:
        type_id_clean = str(type_id or "").strip()
        if not type_id_clean or type_id_clean in updated:
            continue
        updated.append(type_id_clean)
        changed = True
    if not changed:
        return False

    relationships["is_an_instance_of"] = updated
    concept_service.update_concept(concept_id, {"relationships": relationships})
    return True


def _ensure_workflow_texts(workflow_id: str) -> None:
    shared_context = {
        "source": "JVNAUTOSCI-1415",
        "workflow_id": workflow_id,
        "managed_by": "paper_representation_workflow_vontology_service",
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


def _build_publication_registry() -> tuple[
    WorkflowRegistry,
    dict[str, authority_service._CanonicalWorkflowPublicationSpec],
]:
    registry = WorkflowRegistry()
    specs = {
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID: _build_scholarly_workflow_spec(),
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID: _build_arxiv_workflow_spec(),
    }
    for workflow_id, spec in specs.items():
        registry.register(
            WorkflowRegistration(
                workflow_id=workflow_id,
                definition=authority_service._build_definition_from_publication_spec(
                    workflow_id=workflow_id,
                    spec=spec,
                ),
                purpose=_workflow_content(workflow_id),
                source="built_in",
            )
        )
    return registry, specs


def _validate_existing_materialisation(
    *,
    target_workflow_ids: Sequence[str],
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Return whether the published workflow family is already current."""

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


def bootstrap_canonical_paper_representation_workflows() -> dict[str, Any]:
    """Publish and validate the canonical scholarly-paper workflow family."""

    registry, specs = _build_publication_registry()
    target_workflow_ids = tuple(specs.keys())
    already_current, existing_validation_by_workflow_id = (
        _validate_existing_materialisation(target_workflow_ids=target_workflow_ids)
    )

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
        original_specs = dict(authority_service._CANONICAL_WORKFLOW_PUBLICATION_SPECS)
        authority_service._CANONICAL_WORKFLOW_PUBLICATION_SPECS.update(specs)
        try:
            publication_report = authority_service.publish_canonical_chat_workflow_graphs(
                registry=registry,
                target_workflow_ids=target_workflow_ids,
            )
        finally:
            authority_service._CANONICAL_WORKFLOW_PUBLICATION_SPECS.clear()
            authority_service._CANONICAL_WORKFLOW_PUBLICATION_SPECS.update(original_specs)

    typed_workflow_ids: list[str] = []
    typed_step_ids: list[str] = []
    validation_by_workflow_id: dict[str, dict[str, Any]] = {}
    if already_current:
        validation_by_workflow_id.update(existing_validation_by_workflow_id)

    for workflow_id, spec in specs.items():
        if _ensure_instance_typing(concept_id=workflow_id, type_ids=_WORKFLOW_TYPE_IDS):
            typed_workflow_ids.append(workflow_id)
        _ensure_workflow_texts(workflow_id)

        for step_concept_id in _step_concept_ids_for_spec(workflow_id=workflow_id, spec=spec):
            if _ensure_instance_typing(
                concept_id=step_concept_id,
                type_ids=(_WORKFLOW_STEP_TYPE_ID,),
            ):
                typed_step_ids.append(step_concept_id)

        validation = validation_by_workflow_id.get(workflow_id)
        if already_current:
            if not isinstance(validation, dict):
                raise RuntimeError(
                    f"paper_workflow_validation_missing_after_short_circuit:{workflow_id}"
                )
        else:
            graph, graph_warnings = build_workflow_process_graph(workflow_id)
            if not isinstance(graph, dict):
                raise RuntimeError(f"paper_workflow_graph_missing:{workflow_id}")
            warning_items = [
                str(item).strip()
                for item in (graph_warnings or [])
                if isinstance(item, str) and str(item).strip()
            ]
            if warning_items:
                raise RuntimeError(
                    "paper_workflow_graph_warnings_present:"
                    f"{workflow_id}:"
                    + ",".join(warning_items)
                )

            definition = load_workflow_definition_from_vontology(workflow_id)
            if definition is None:
                raise RuntimeError(
                    f"paper_workflow_definition_not_loadable:{workflow_id}"
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
                "paper_workflow_validation_failed:"
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
        "publication": publication_report,
        "typed_workflow_ids": typed_workflow_ids,
        "typed_step_ids": typed_step_ids,
        "validation_by_workflow_id": validation_by_workflow_id,
    }


__all__ = [
    "ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID",
    "SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID",
    "bootstrap_canonical_paper_representation_workflows",
]
