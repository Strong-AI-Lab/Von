"""Materialise the canonical workflow-aware model-selection subworkflow."""

from __future__ import annotations

from typing import Any

from ..workflows import workflow_concept_authority_service as authority_service
from ..workflows.definitions import WORKFLOW_MODEL_SELECTION_WORKFLOW_ID
from ..workflows.durable.model_selection_workflow import (
    WORKFLOW_MODEL_SELECTION_ACTION_ID,
)
from ..workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
)


_MANAGED_BY = "workflow_model_selection_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2266"
_WORKFLOW_NAME = "Workflow-aware model selection"
_WORKFLOW_DESCRIPTION = (
    "Resolves the enabled model pool and the selected model candidate for a "
    "workflow stage using the Vontology-backed workflow model policy. The "
    "subworkflow keeps model-routing policy represented while Python supplies "
    "only the generic candidate-pool and policy-resolution support surface."
)


def _build_authoring_spec() -> dict[str, Any]:
    return {
        "workflow_id": WORKFLOW_MODEL_SELECTION_WORKFLOW_ID,
        "workflow_name": _WORKFLOW_NAME,
        "workflow_description": _WORKFLOW_DESCRIPTION,
        "parent_type_id": "#V#durable_workflow",
        "initial_state_key": "select_model",
        "steps": [
            {
                "state_id": "select_model",
                "state_key": "select_model",
                "action_id": WORKFLOW_MODEL_SELECTION_ACTION_ID,
                "execution_mode": "deterministic",
                "context_input_mappings": [
                    {
                        "tool_param": "stage",
                        "context_key": "stage",
                        "required": False,
                    },
                    {
                        "tool_param": "policy_stage",
                        "context_key": "policy_stage",
                        "required": False,
                    },
                    {
                        "tool_param": "workflow_id",
                        "context_key": "workflow_id",
                        "required": False,
                    },
                    {
                        "tool_param": "selected_workflow_id",
                        "context_key": "selected_workflow_id",
                        "required": False,
                    },
                    {
                        "tool_param": "default_model",
                        "context_key": "default_model",
                        "required": False,
                    },
                    {
                        "tool_param": "prefer_default_model",
                        "context_key": "prefer_default_model",
                        "required": False,
                    },
                    {
                        "tool_param": "user_concept_id",
                        "context_key": "user_concept_id",
                        "required": False,
                    },
                    {
                        "tool_param": "organisation_concept_id",
                        "context_key": "organisation_concept_id",
                        "required": False,
                    },
                ],
                "writes_context_keys": [
                    "model_selection",
                    "selected_model",
                    "selected_model_provider",
                    "selected_candidate",
                    "model_candidate_pool",
                    "enabled_model_pool",
                    "workflow_model_policy",
                    "selection_metadata",
                ],
                "tool_output_context_mappings": [
                    {
                        "tool_output_field": "model_selection",
                        "context_key": "model_selection",
                    },
                    {
                        "tool_output_field": "selected_model",
                        "context_key": "selected_model",
                    },
                    {
                        "tool_output_field": "selected_model_provider",
                        "context_key": "selected_model_provider",
                    },
                    {
                        "tool_output_field": "selected_candidate",
                        "context_key": "selected_candidate",
                    },
                    {
                        "tool_output_field": "model_candidate_pool",
                        "context_key": "model_candidate_pool",
                    },
                    {
                        "tool_output_field": "enabled_model_pool",
                        "context_key": "enabled_model_pool",
                    },
                    {
                        "tool_output_field": "workflow_model_policy",
                        "context_key": "workflow_model_policy",
                    },
                    {
                        "tool_output_field": "selection_metadata",
                        "context_key": "selection_metadata",
                    },
                ],
                "next_state_key": "done",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "done",
                "state_key": "done",
                "terminal": True,
            },
            {
                "state_id": "failed",
                "state_key": "failed",
                "terminal": True,
            },
        ],
    }


def bootstrap_canonical_workflow_model_selection_workflow(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish and validate the canonical model-selection subworkflow."""

    definition = build_workflow_definition_from_authoring_spec(_build_authoring_spec())
    report = authority_service.publish_workflow_definition_from_definition(
        definition=definition,
        create_missing=True,
        purpose=_WORKFLOW_DESCRIPTION,
    )
    errors_by_workflow_id = report.get("errors_by_workflow_id") or {}
    validation_failures = report.get("validation_failures_by_workflow_id") or {}
    workflow_error = (
        errors_by_workflow_id.get(WORKFLOW_MODEL_SELECTION_WORKFLOW_ID)
        if isinstance(errors_by_workflow_id, dict)
        else None
    )
    validation_failure = (
        validation_failures.get(WORKFLOW_MODEL_SELECTION_WORKFLOW_ID)
        if isinstance(validation_failures, dict)
        else None
    )
    success = not workflow_error and not validation_failure

    if force_republish or success:
        authority_service.upsert_workflow_publication_lifecycle(
            workflow_id=WORKFLOW_MODEL_SELECTION_WORKFLOW_ID,
            phase="published" if success else "draft",
            published=bool(success),
            validation_passed=bool(success),
            postconditions_verified=bool(success),
        )

    return {
        "success": bool(success),
        "workflow_ids": [WORKFLOW_MODEL_SELECTION_WORKFLOW_ID],
        "source": _SOURCE_TAG,
        "managed_by": _MANAGED_BY,
        "publication": report,
        "error": workflow_error,
        "validation_failure": validation_failure,
    }


__all__ = ["bootstrap_canonical_workflow_model_selection_workflow"]