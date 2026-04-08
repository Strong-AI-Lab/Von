"""Action handlers for the Master Conversation-Turn workflow.

JVNAUTOSCI-1763:
- Cede turn control from Python routes to durable VWL workflows.
- Support supervised execution of capability workflows (e.g. arXiv).
- Close the 'communicative gap' by reporting outcomes from completion reports.
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
from ..execution_contracts import (
    WORKFLOW_STEP_RESULT_ENVELOPE_SCHEMA_VERSION,
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

        selected_workflow_id = request.data.get("selected_workflow_id")
        if not selected_workflow_id:
            return WorkflowActionResult(
                status="failed",
                error="turn_execution_no_workflow_selected",
            )

        subworkflow_inputs = {
            "workflow_id": selected_workflow_id,
            **{
                str(key): value
                for key, value in request.data.items()
                if isinstance(key, str)
            },
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
        if not subworkflow_result.ok:
            return subworkflow_result

        child_result = subworkflow_result.outputs.get("result")
        child_payload = dict(child_result) if isinstance(child_result, Mapping) else {}
        completion_report = child_payload.get("completion_report")
        if not isinstance(completion_report, Mapping):
            response_text = child_payload.get("response_text")
            if not isinstance(response_text, str) or not response_text.strip():
                response_text = child_payload.get("final_response")
            completion_report = {
                "schema_version": "conversation_turn_selected_workflow_result.v1",
                "workflow_id": selected_workflow_id,
                "response_text": (
                    response_text.strip()
                    if isinstance(response_text, str) and response_text.strip()
                    else None
                ),
            }
        outputs = {
            "selected_workflow_id": selected_workflow_id,
            "completion_report": dict(completion_report),
        }
        if isinstance(child_payload.get("final_response"), str):
            outputs["final_response"] = child_payload.get("final_response")
        if isinstance(child_payload.get("current_response"), str):
            outputs["current_response"] = child_payload.get("current_response")
        return WorkflowActionResult(outputs=outputs)
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

    # Note: Critic and Completion Gate are often synthetic engine-level actions
    # but can be registered here if they need custom durable implementations.
