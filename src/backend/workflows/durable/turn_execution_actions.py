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
                "is_arxiv": selected_workflow_id == "#V#arxiv_paper_representation_workflow"
            }
        )
    return _handle


def _build_turn_execution_execute_selected_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        """Execute the selected capability workflow with supervision."""
        selected_workflow_id = request.data.get("selected_workflow_id")
        if not selected_workflow_id:
            return WorkflowActionResult(
                status="failed",
                error="turn_execution_no_workflow_selected",
            )

        # In Phase B, we use the subworkflow action to run the selected workflow.
        # The 'step_callback' in the environment will handle real-time events.
        from .subworkflow_actions import _build_subworkflow_handler
        from ..registry_factory import get_workflow_registry
        
        # We wrap the existing subworkflow handler
        registry = get_workflow_registry()
        sub_handler = _build_subworkflow_handler(
            registry=registry.action_registry,
            definition_loader=registry.get,
        )
        
        # We need to construct a WorkflowActionRequest for the subworkflow
        # but since we are already in an action, we can just call it?
        # Actually, it's better to use the subworkflow action ID if it's registered.
        
        return registry.action_registry.execute(
            "workflow_invoke_subworkflow",
            inputs={"workflow_id": selected_workflow_id},
            context=request.data,
            env=request.environment,
            trace=request.trace,
            workflow_id=request.workflow_id,
            workflow_state_id=request.workflow_state_id,
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

    # Note: Critic and Completion Gate are often synthetic engine-level actions
    # but can be registered here if they need custom durable implementations.
