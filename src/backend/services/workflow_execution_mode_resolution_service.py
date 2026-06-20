"""Support helpers for selected-workflow execution-mode resolution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SelectedWorkflowExecutionModeResolution:
    """Execution-mode decision plus the represented/support source used."""

    execution_mode: str | None
    authority_source: str | None


def normalise_declared_workflow_execution_mode(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"custom_workflow", "direct_response", "tool_pipeline"}:
        return text
    return None


def resolve_declared_workflow_execution_mode_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> SelectedWorkflowExecutionModeResolution:
    """Resolve represented execution-mode metadata without applying defaults."""

    if not isinstance(metadata, Mapping):
        return SelectedWorkflowExecutionModeResolution(None, None)

    for source_label, source in (
        ("workflow_metadata", metadata),
        ("workflow_metadata.routing_profile", metadata.get("routing_profile")),
        (
            "workflow_metadata.workflow_execution_contract",
            metadata.get("workflow_execution_contract"),
        ),
    ):
        if not isinstance(source, Mapping):
            continue
        for key in (
            "execution_mode",
            "selected_execution_mode",
            "dispatch_execution_mode",
        ):
            execution_mode = normalise_declared_workflow_execution_mode(
                source.get(key)
            )
            if execution_mode:
                return SelectedWorkflowExecutionModeResolution(
                    execution_mode,
                    f"{source_label}.{key}",
                )
    return SelectedWorkflowExecutionModeResolution(None, None)


def resolve_selected_workflow_execution_mode(
    *,
    selected_workflow_id: str | None,
    workflow_metadata_resolver: Callable[[str], Mapping[str, Any] | None],
    action_contract_matches: Callable[[str, frozenset[str]], bool],
    tool_pipeline_action_ids: frozenset[str],
    narration_action_ids: frozenset[str],
    tool_calling_workflow_id: str,
    chat_assistant_workflow_id: str,
    chat_narration_workflow_id: str,
) -> SelectedWorkflowExecutionModeResolution:
    """Resolve selected execution mode in represented-authority order."""

    workflow_id = (
        selected_workflow_id.strip()
        if isinstance(selected_workflow_id, str) and selected_workflow_id.strip()
        else None
    )
    if not workflow_id:
        return SelectedWorkflowExecutionModeResolution(None, None)

    declared_resolution = resolve_declared_workflow_execution_mode_from_metadata(
        workflow_metadata_resolver(workflow_id)
    )
    if declared_resolution.execution_mode:
        return declared_resolution

    if action_contract_matches(workflow_id, tool_pipeline_action_ids):
        return SelectedWorkflowExecutionModeResolution(
            "tool_pipeline",
            "action_contract:tool_pipeline",
        )
    if action_contract_matches(workflow_id, narration_action_ids):
        return SelectedWorkflowExecutionModeResolution(
            "direct_response",
            "action_contract:direct_response",
        )

    if workflow_id == tool_calling_workflow_id:
        return SelectedWorkflowExecutionModeResolution(
            "tool_pipeline",
            "builtin_support_default:tool_calling_workflow",
        )
    if workflow_id == chat_assistant_workflow_id:
        return SelectedWorkflowExecutionModeResolution(
            "direct_response",
            "builtin_support_default:chat_assistant_workflow",
        )
    if workflow_id == chat_narration_workflow_id:
        return SelectedWorkflowExecutionModeResolution(
            "direct_response",
            "builtin_support_default:chat_narration_workflow",
        )

    return SelectedWorkflowExecutionModeResolution(
        "custom_workflow",
        "generic_support_default:custom_workflow",
    )
