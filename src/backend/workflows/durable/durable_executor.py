"""Durable workflow executor with checkpoint support.

Extends the base WorkflowExecutor to persist state after each step,
enabling resume from the last checkpoint after interruption.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from ..engine import (
    WorkflowDefinition,
    state_has_on_failure_transition,
)
from ..metadata_validation import (
    append_metadata_validation_event,
    format_metadata_validation_error,
    skipped_metadata_validation,
    validate_state_metadata_post_action,
    validate_state_metadata_pre_action,
)
from ..action_registry import ActionRegistry, WorkflowEnvironment
from ..trace_model import WorkflowExecutionTrace
from .instance_manager import WorkflowInstanceManager
from .models import WorkflowInstance, WorkflowInstanceStatus

logger = logging.getLogger(__name__)


@dataclass
class DurableWorkflowResult:
    """Result of a durable workflow execution."""

    instance_id: str
    data: dict[str, Any]
    completed: bool
    final_state: str
    error: str | None = None
    step_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serialisable dict."""
        return {
            "instance_id": self.instance_id,
            "data": self.data,
            "completed": self.completed,
            "final_state": self.final_state,
            "error": self.error,
            "step_count": self.step_count,
        }


class DurableWorkflowExecutor:
    """Execute workflows with checkpoint-based durability.

    Extends the standard WorkflowExecutor pattern with:
    - State persistence after each step
    - Resume from last checkpoint on restart
    - Cancellation checking between steps
    - Lock heartbeat extension during long steps
    """

    def __init__(
        self,
        *,
        registry: ActionRegistry,
        instance_manager: WorkflowInstanceManager,
        max_transitions: int = 50,
    ) -> None:
        """Initialise the durable executor.

        Args:
            registry: Action registry for executing workflow actions.
            instance_manager: Manager for instance persistence.
            max_transitions: Maximum state transitions before error.
        """
        self._registry = registry
        self._instance_manager = instance_manager
        self._max_transitions = max(5, int(max_transitions))

    def run_durable(
        self,
        instance_id: str,
        definition: WorkflowDefinition,
        *,
        worker_id: str | None = None,
        resume_from_checkpoint: bool = True,
    ) -> DurableWorkflowResult:
        """Execute a workflow with checkpointing.

        Args:
            instance_id: The workflow instance ID.
            definition: The workflow definition to execute.
            worker_id: Optional worker ID for lock extension.
            resume_from_checkpoint: Whether to resume from saved state.

        Returns:
            DurableWorkflowResult with execution outcome.
        """
        # Load instance
        instance = self._instance_manager.get_instance(instance_id)
        if instance is None:
            return DurableWorkflowResult(
                instance_id=instance_id,
                data={},
                completed=False,
                final_state="",
                error="instance_not_found",
            )

        # Restore or initialise state
        if resume_from_checkpoint and instance.workflow_data:
            context = dict(instance.workflow_data)
            current_state = instance.current_state or definition.initial_state
            step_index = instance.step_index
        else:
            context = dict(instance.inputs)
            current_state = definition.initial_state
            step_index = 0

        # Create execution environment
        from ...languagemodels.llm_interface import (
            get_active_model_name,
            get_llm_client,
        )

        llm_client = get_llm_client(
            user_concept_id=instance.user_id,
            org_concept_id=instance.org_id,
        )
        environment = WorkflowEnvironment(
            llm_client=llm_client,
            model=get_active_model_name(),
            user_namespace=instance.namespace,
        )

        # Create trace for observability
        trace = WorkflowExecutionTrace(
            workflow_id=definition.workflow_id,
            execution_id=str(uuid.uuid4()),
            user_namespace=instance.namespace,
        )

        # Compute terminal states
        termination_states = set(definition.termination_states) | {
            state_id for state_id, spec in definition.states.items() if spec.terminal
        }
        total_steps = max(1, len(definition.states))

        transitions = 0
        while transitions < self._max_transitions:
            transitions += 1
            step_index += 1

            # Check for cancellation
            if self._instance_manager.is_cancelled(instance_id):
                return DurableWorkflowResult(
                    instance_id=instance_id,
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error="cancelled",
                    step_count=step_index,
                )

            # Extend lock if worker_id provided
            if worker_id:
                self._instance_manager.extend_lock(instance_id, worker_id)

            # Get current state spec
            state_spec = definition.states.get(current_state)
            if state_spec is None:
                error = f"unknown_state:{current_state}"
                self._instance_manager.checkpoint(
                    instance_id,
                    current_state=current_state,
                    workflow_data=context,
                    step_index=step_index,
                    error=error,
                    progress_current=step_index,
                    progress_total=total_steps,
                    progress_message=current_state,
                )
                return DurableWorkflowResult(
                    instance_id=instance_id,
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error=error,
                    step_count=step_index,
                )

            # Record state entry in trace
            trace.record_state_transition(
                current_state,
                current_state,
                verdict={"status": "enter"},
            )

            pre_validation = validate_state_metadata_pre_action(
                state_id=current_state,
                metadata=state_spec.metadata,
                context=context,
            )
            append_metadata_validation_event(context=context, result=pre_validation)
            if pre_validation.applied:
                trace.record_state_transition(
                    current_state,
                    current_state,
                    verdict=pre_validation.to_trace_verdict(),
                )
            if not pre_validation.ok:
                error = format_metadata_validation_error(pre_validation)
                self._instance_manager.checkpoint(
                    instance_id,
                    current_state=current_state,
                    workflow_data=context,
                    step_index=step_index,
                    error=error,
                    progress_current=step_index,
                    progress_total=total_steps,
                    progress_message=current_state,
                )
                trace.finish_failed(error)
                return DurableWorkflowResult(
                    instance_id=instance_id,
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error=error,
                    step_count=step_index,
                )

            # Execute actions
            state_has_failure_route = state_has_on_failure_transition(state_spec)
            context_before_actions = dict(context)
            for action in state_spec.actions:
                result = self._registry.execute(
                    action.action_id,
                    inputs=action.inputs,
                    context=context,
                    env=environment,
                    trace=trace,
                )

                trace.record_action(
                    action_id=action.action_id,
                    inputs=action.inputs,
                    outputs=result.outputs,
                    status=result.status,
                    error=result.error,
                    call_id=result.call_id,
                    duration_ms=result.duration_ms,
                )

                if not result.ok:
                    if state_has_failure_route:
                        # Safety envelope (JVNAUTOSCI-1087): if the state defines
                        # an explicit on_failure branch, do not fail-fast here.
                        # Keep failure markers in context and evaluate transitions.
                        break
                    error = result.error or "action_failed"
                    # Checkpoint the failure state
                    self._instance_manager.checkpoint(
                        instance_id,
                        current_state=current_state,
                        workflow_data=context,
                        step_index=step_index,
                        error=error,
                        error_step=action.action_id,
                        progress_current=step_index,
                        progress_total=total_steps,
                        progress_message=current_state,
                    )
                    trace.finish_failed(error)
                    return DurableWorkflowResult(
                        instance_id=instance_id,
                        data=context,
                        completed=False,
                        final_state=current_state,
                        error=error,
                        step_count=step_index,
                    )

                context.update(result.outputs)

            if state_has_failure_route and bool(context.get("last_action_failed")):
                post_validation = skipped_metadata_validation(
                    state_id=current_state,
                    phase="post_action",
                    reason="action_failed_with_on_failure_route",
                )
            else:
                post_validation = validate_state_metadata_post_action(
                    state_id=current_state,
                    metadata=state_spec.metadata,
                    context_before=context_before_actions,
                    context_after=context,
                )

            append_metadata_validation_event(context=context, result=post_validation)
            if post_validation.applied:
                trace.record_state_transition(
                    current_state,
                    current_state,
                    verdict=post_validation.to_trace_verdict(),
                )
            if not post_validation.ok:
                error = format_metadata_validation_error(post_validation)
                self._instance_manager.checkpoint(
                    instance_id,
                    current_state=current_state,
                    workflow_data=context,
                    step_index=step_index,
                    error=error,
                    progress_current=step_index,
                    progress_total=total_steps,
                    progress_message=current_state,
                )
                trace.finish_failed(error)
                return DurableWorkflowResult(
                    instance_id=instance_id,
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error=error,
                    step_count=step_index,
                )

            # Check for terminal state
            if current_state in termination_states:
                # Final checkpoint
                self._instance_manager.checkpoint(
                    instance_id,
                    current_state=current_state,
                    workflow_data=context,
                    step_index=step_index,
                    progress_current=step_index,
                    progress_total=total_steps,
                    progress_message=current_state,
                )
                trace.finish_completed()
                return DurableWorkflowResult(
                    instance_id=instance_id,
                    data=context,
                    completed=True,
                    final_state=current_state,
                    step_count=step_index,
                )

            # Evaluate transitions
            next_state = None
            transition_reason = None
            for transition in state_spec.transitions:
                try:
                    if transition.condition(context):
                        next_state = transition.to_state
                        transition_reason = transition.reason
                        break
                except Exception as e:
                    logger.warning(
                        "[durable_workflow] Transition condition error in %s: %s",
                        current_state,
                        e,
                    )
                    continue

            if next_state is None:
                error = "no_transition"
                self._instance_manager.checkpoint(
                    instance_id,
                    current_state=current_state,
                    workflow_data=context,
                    step_index=step_index,
                    error=error,
                    progress_current=step_index,
                    progress_total=total_steps,
                    progress_message=current_state,
                )
                trace.finish_failed(error)
                return DurableWorkflowResult(
                    instance_id=instance_id,
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error=error,
                    step_count=step_index,
                )

            # CHECKPOINT after successful step (before transitioning)
            self._instance_manager.checkpoint(
                instance_id,
                current_state=next_state,
                workflow_data=context,
                step_index=step_index,
                progress_current=step_index,
                progress_total=total_steps,
                progress_message=next_state,
            )

            trace.record_state_transition(
                current_state,
                next_state,
                reason=transition_reason,
            )
            current_state = next_state

        # Transition limit exceeded
        error = "transition_limit"
        self._instance_manager.checkpoint(
            instance_id,
            current_state=current_state,
            workflow_data=context,
            step_index=step_index,
            error=error,
            progress_current=step_index,
            progress_total=total_steps,
            progress_message=current_state,
        )
        trace.finish_failed(error)
        return DurableWorkflowResult(
            instance_id=instance_id,
            data=context,
            completed=False,
            final_state=current_state,
            error=error,
            step_count=step_index,
        )

    def run_new_instance(
        self,
        definition: WorkflowDefinition,
        *,
        user_id: str,
        org_id: str,
        namespace: str,
        inputs: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> DurableWorkflowResult:
        """Create and execute a new workflow instance.

        Convenience method that creates an instance and immediately runs it.

        Args:
            definition: The workflow definition to execute.
            user_id: User initiating the workflow.
            org_id: Organisation context.
            namespace: Data access namespace.
            inputs: Initial workflow inputs.
            worker_id: Optional worker ID for lock management.

        Returns:
            DurableWorkflowResult with execution outcome.
        """
        instance_id = self._instance_manager.create_instance(
            definition.workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=inputs,
        )

        return self.run_durable(
            instance_id,
            definition,
            worker_id=worker_id,
            resume_from_checkpoint=False,
        )
