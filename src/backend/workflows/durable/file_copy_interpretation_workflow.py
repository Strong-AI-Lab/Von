"""Durable workflow for uploaded file-copy interpretation (JVNAUTOSCI-1302).

This workflow is intentionally MCP-tool driven so behaviour can evolve through
Vontology-bound tool orchestration without adding specialised Python handlers.
"""

from __future__ import annotations

from ..action_registry import ActionRegistry
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..workflow_registry import WorkflowRegistration

FILE_COPY_INTERPRETATION_WORKFLOW_ID = "#V#file_copy_interpretation_workflow"
_FILE_COPY_CONTEXT_INPUTS = {
    "concept_id": {
        "$context_key": "concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_concept_id_to_concept_id_parameter",
    },
    "file_copy_concept_id": {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_file_copy_concept_id_to_file_copy_concept_id_parameter",
    },
}


def build_file_copy_interpretation_workflow_test_definition() -> WorkflowDefinition:
    interpret = WorkflowStateSpec(
        state_id="interpret",
        actions=(
            WorkflowActionInvocation(
                action_id="interpret_file_copy",
                inputs=dict(_FILE_COPY_CONTEXT_INPUTS),
                description=(
                    "Extract structured interpretation from a blob-backed "
                    "#V#computer_file_copy and persist canonical text relations."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="index",
                condition=lambda ctx: bool(ctx.get("index_in_rag", True)),
                reason="index_enabled",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="index_skipped",
            ),
        ),
    )

    index = WorkflowStateSpec(
        state_id="index",
        actions=(
            WorkflowActionInvocation(
                action_id="index_file_copy",
                inputs=dict(_FILE_COPY_CONTEXT_INPUTS),
                description="Index extracted file-copy text into RAG for the namespace.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="index_finished",
            ),
        ),
    )

    complete = WorkflowStateSpec(state_id="complete", terminal=True)
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=FILE_COPY_INTERPRETATION_WORKFLOW_ID,
        initial_state="interpret",
        states={
            "interpret": interpret,
            "index": index,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Interpret newly uploaded file-copy content (including screenshots/"
            "images) and persist rich concept descriptions, then index in RAG."
        ),
    )


def build_file_copy_interpretation_workflow_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=FILE_COPY_INTERPRETATION_WORKFLOW_ID,
        definition=build_file_copy_interpretation_workflow_test_definition(),
        purpose=(
            "Interpret uploaded file copies with image/document extraction and "
            "persisted concept enrichment."
        ),
        source="built_in",
    )


def register_file_copy_interpretation_actions(_registry: ActionRegistry) -> None:
    """No-op: this workflow intentionally relies on MCP fallback actions."""

    return None
