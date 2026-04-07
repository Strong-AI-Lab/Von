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
TURN_EXECUTION_NARRATE_ACTION_ID = "turn_execution.narrate"
TURN_EXECUTION_CRITIC_ACTION_ID = "turn_execution.critic"
TURN_EXECUTION_COMPLETION_GATE_ACTION_ID = "turn_execution.completion_gate"


def _build_turn_execution_route_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        """Resolve the workflow routing for the current turn."""
        from ...integrations.internal_mcp.orchestrator import (
            WorkflowRoutingInfo,
        )
        from ...server.routes.von_routes import (
            assess_workflow_routing_candidate_policy,
        )

        # 1. Use existing discovery results if provided, else perform discovery
        discovery = request.data.get("workflow_discovery")
        prompt = request.data.get("user_prompt")
        
        # TODO: Implement real discovery/selection logic here if not already done.
        # For Phase B, we trust the discovery passed in from the route.
        selected_workflow_id = discovery.get("selected_workflow_id")
        
        return WorkflowActionResult(
            status="success",
            outputs={
                "selected_workflow_id": selected_workflow_id,
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


def _build_turn_execution_narrate_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        """Generate the final assistant narration based on workflow outcomes."""
        # This closes the 'communicative gap' by looking at completion reports.
        completion_report = request.data.get("completion_report")
        prompt = request.data.get("user_prompt")
        
        if not completion_report:
            # Fallback to standard narration if no structured report exists.
            return WorkflowActionResult(status="success")
        
        # Phase C: Use the LLM to narrate the structured completion report.
        # We provide the report and the user's original prompt.
        report_json = str(completion_report)
        
        system_prompt = (
            "You are a helpful assistant. Your task is to narrate the results of a workflow "
            "execution based on a structured completion report. Be specific about what "
            "was actually done (e.g. concepts created, files reused). Be operationally truthful."
        )
        
        user_msg = (
            f"User asked: {prompt}\n\n"
            f"Workflow Completion Report: {report_json}\n\n"
            "Please provide a concise, truthful summary of what was accomplished."
        )
        
        try:
            response = request.environment.llm_client.generate(
                model=None,  # Use default
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
            narrated_text = response.text
        except Exception as exc:
            logger.warning("Narration failed: %s", exc)
            # Fallback
            status = completion_report.get("verification_status", "unknown")
            narrated_text = f"Ingestion completed with status: {status}."

        return WorkflowActionResult(
            status="success",
            outputs={
                "response_text": narrated_text,
                "narration_prepared": True
            }
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
            action_id=TURN_EXECUTION_NARRATE_ACTION_ID,
            handler=_build_turn_execution_narrate_handler(),
            description="Generate final assistant narration from workflow outcomes.",
        )
    )

    # Note: Critic and Completion Gate are often synthetic engine-level actions
    # but can be registered here if they need custom durable implementations.
