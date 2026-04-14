"""Action handlers for the Master Conversation-Turn workflow.

JVNAUTOSCI-1763:
- Cede turn control from Python routes to durable VWL workflows.
- Support supervised execution of capability workflows (e.g. arXiv).
- Keep selected-workflow answer content separate from execution reporting.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ..action_registry import (
    ActionSpec,
    ActionRegistry,
    WorkflowActionResult,
    WorkflowActionRequest,
)
from ..subworkflow_contracts import WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE
from .turn_execution_runtime_support import (
    build_turn_execution_selected_workflow_outputs,
    run_turn_execution_completion_gate,
    run_turn_execution_critic,
)

logger = logging.getLogger(__name__)

TURN_EXECUTION_ROUTE_ACTION_ID = "turn_execution.route"
TURN_EXECUTION_EXECUTE_SELECTED_ACTION_ID = "turn_execution.execute_selected"
TURN_EXECUTION_CRITIC_ACTION_ID = "turn_execution.critic"
TURN_EXECUTION_COMPLETION_GATE_ACTION_ID = "turn_execution.completion_gate"


def _build_turn_execution_route_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        """Resolve the workflow routing for the current turn."""
        from ...services.workflow_discovery_service import (
            discover_workflows_for_turn,
        )

        # 1. Use existing discovery results if provided, else perform discovery
        discovery = request.data.get("workflow_discovery")
        prompt = request.data.get("user_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = request.data.get("prompt")
        prompt = prompt.strip() if isinstance(prompt, str) else ""

        if not discovery or not discovery.get("selected_workflow_id"):
            discovery_result = discover_workflows_for_turn(
                prompt,
                namespace=request.environment.user_namespace,
            )
            discovery = discovery_result or {}

        selected_workflow_id = discovery.get("selected_workflow_id")
        
        # If still no workflow, we might want to default to a generic one
        # but for now we follow the existing logic.
        
        return WorkflowActionResult(
            status="success",
            outputs={
                "selected_workflow_id": selected_workflow_id,
                "workflow_discovery": discovery,
            }
        )
    return _handle


def _build_turn_execution_execute_selected_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        """Execute the selected capability workflow with supervision."""
        from .registry_factory import get_shared_durable_action_registry

        selected_workflow_id_raw = request.data.get("selected_workflow_id")
        selected_workflow_id = (
            str(selected_workflow_id_raw).strip()
            if isinstance(selected_workflow_id_raw, str)
            and str(selected_workflow_id_raw).strip()
            else None
        )
        if not selected_workflow_id:
            outputs = build_turn_execution_selected_workflow_outputs(
                selected_workflow_id=None,
                child_completed=False,
                final_state="no_selected_workflow",
                failure_detail="turn_execution_no_workflow_selected",
                child_outputs={},
                child_result_snapshot={},
                selected_workflow_trace=request.data.get("selected_workflow_trace"),
                workflow_routing=request.data.get("workflow_routing"),
                workflow_discovery=(
                    request.data.get("workflow_discovery_result")
                    if isinstance(request.data.get("workflow_discovery_result"), Mapping)
                    else request.data.get("workflow_discovery")
                ),
            )
            return WorkflowActionResult(outputs=outputs)

        request_data = {
            str(key): value for key, value in request.data.items() if isinstance(key, str)
        }
        continuation_context = request_data.get("continuation_context")
        projected_continuation_launch_inputs: dict[str, Any] = {}
        if isinstance(continuation_context, Mapping) and bool(
            continuation_context.get("applied")
        ):
            try:
                from ...services.workflow_continuation_service import (
                    project_launch_inputs_from_continuation_context,
                )

                projected_continuation_launch_inputs = (
                    project_launch_inputs_from_continuation_context(
                        continuation_context
                    )
                )
            except Exception:
                projected_continuation_launch_inputs = {}
        if projected_continuation_launch_inputs:
            if isinstance(request.data, dict):
                request.data.setdefault(
                    "workflow_continuation_launch_inputs",
                    dict(projected_continuation_launch_inputs),
                )
                for key, value in projected_continuation_launch_inputs.items():
                    request.data.setdefault(key, value)
            request_data["workflow_continuation_launch_inputs"] = dict(
                projected_continuation_launch_inputs
            )
            for key, value in projected_continuation_launch_inputs.items():
                request_data.setdefault(key, value)

        subworkflow_inputs = {
            "workflow_id": selected_workflow_id,
            "failure_mode": WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
            **request_data,
        }
        subworkflow_result = get_shared_durable_action_registry().execute(
            "workflow_invoke_subworkflow",
            inputs=subworkflow_inputs,
            context=request.data,
            env=request.environment,
            trace=request.trace,
            workflow_id=request.workflow_id,
            workflow_state_id=request.workflow_state_id,
        )
        child_payload: dict[str, Any] = {}
        child_completed = False
        child_final_state: str | None = None
        child_error: str | None = None

        if subworkflow_result.ok:
            child_result = subworkflow_result.outputs.get("result")
            child_payload = dict(child_result) if isinstance(child_result, Mapping) else {}
            invocation = subworkflow_result.outputs.get("subworkflow_invocation")
            invocation_payload = (
                dict(invocation) if isinstance(invocation, Mapping) else {}
            )
            child_completed = not bool(
                subworkflow_result.outputs.get("child_workflow_failed")
            )
            child_final_state = (
                str(invocation_payload.get("child_final_state")).strip()
                if isinstance(invocation_payload.get("child_final_state"), str)
                and str(invocation_payload.get("child_final_state")).strip()
                else None
            )
            child_error = (
                str(subworkflow_result.outputs.get("subworkflow_error")).strip()
                if isinstance(subworkflow_result.outputs.get("subworkflow_error"), str)
                and str(subworkflow_result.outputs.get("subworkflow_error")).strip()
                else (
                    str(invocation_payload.get("child_error")).strip()
                    if isinstance(invocation_payload.get("child_error"), str)
                    and str(invocation_payload.get("child_error")).strip()
                    else None
                )
            )
        else:
            child_error = (
                str(subworkflow_result.error).strip()
                if isinstance(subworkflow_result.error, str)
                and str(subworkflow_result.error).strip()
                else "selected_workflow_execution_failed"
            )
            child_final_state = "subworkflow_invocation_failed"

        outputs = build_turn_execution_selected_workflow_outputs(
            selected_workflow_id=selected_workflow_id,
            child_completed=child_completed,
            final_state=child_final_state,
            failure_detail=child_error,
            child_outputs=child_payload,
            child_result_snapshot=child_payload,
            selected_workflow_trace=request.data.get("selected_workflow_trace"),
            workflow_routing=request.data.get("workflow_routing"),
            workflow_discovery=(
                request.data.get("workflow_discovery_result")
                if isinstance(request.data.get("workflow_discovery_result"), Mapping)
                else request.data.get("workflow_discovery")
            ),
        )
        return WorkflowActionResult(outputs=outputs)
    return _handle


def _build_turn_execution_critic_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        return run_turn_execution_critic(
            request,
            annotation_component="durable_turn_execution_actions",
            annotation_function="_build_turn_execution_critic_handler",
        )

    return _handle


def _build_turn_execution_completion_gate_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        return run_turn_execution_completion_gate(
            request,
            annotation_component="durable_turn_execution_actions",
            annotation_function="_build_turn_execution_completion_gate_handler",
            introspection_auto_apply_env="VON_WORKFLOW_INTROSPECTION_AUTO_APPLY",
        )

    return _handle


def register_turn_execution_actions(registry: ActionRegistry) -> None:
    """Register all master-turn control plane actions."""
    
    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_ROUTE_ACTION_ID,
            handler=_build_turn_execution_route_handler(),
            description="Resolve workflow routing for the current turn.",
        )
    )
    
    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_EXECUTE_SELECTED_ACTION_ID,
            handler=_build_turn_execution_execute_selected_handler(),
            description="Execute the selected capability workflow with supervision.",
        )
    )

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_CRITIC_ACTION_ID,
            handler=_build_turn_execution_critic_handler(),
            description="Evaluate required effects and postcondition checks for the turn.",
        )
    )

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_COMPLETION_GATE_ACTION_ID,
            handler=_build_turn_execution_completion_gate_handler(),
            description="Apply completion gate and prevent false completion claims.",
        )
    )
