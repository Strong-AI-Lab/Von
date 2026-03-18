"""Durable workflow executor with checkpoint support.

Extends the base WorkflowExecutor to persist state after each step,
enabling resume from the last checkpoint after interruption.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from ..engine import (
    WorkflowDefinition,
    apply_tool_output_context_mappings,
    execute_workflow_step_invocation,
    evaluate_transition_condition_spec,
    materialise_terminal_effect_context,
    resolve_action_inputs_from_context,
    state_has_on_break_transition,
    state_has_on_continue_transition,
    state_has_on_failure_transition,
    state_has_on_unknown_transition,
)
from ..metadata_validation import (
    METADATA_VALIDATION_MODE_OFF,
    apply_metadata_validation_mode,
    append_metadata_validation_event,
    format_metadata_validation_error,
    get_metadata_validation_mode,
    metadata_validation_failures_are_enforced,
    skipped_metadata_validation,
    validate_state_metadata_post_action,
    validate_state_metadata_pre_action,
)
from ..action_registry import (
    ActionRegistry,
    WORKFLOW_ACTION_OUTCOME_FAILURE,
    WORKFLOW_ACTION_OUTCOME_UNKNOWN,
    WorkflowEnvironment,
    normalise_action_outcome,
)
from ..trace_model import WorkflowExecutionTrace
from ..execution_contracts import (
    WORKFLOW_CONTROL_SIGNAL_BREAK,
    WORKFLOW_CONTROL_SIGNAL_CONTINUE,
    WORKFLOW_CONTROL_SIGNAL_RETURN,
    WORKFLOW_RETURN_PAYLOAD_KEY,
    append_runtime_event,
    append_step_result_envelope,
    build_step_result_envelope,
    build_workflow_result_envelope,
    clear_control_signal_context,
    get_last_control_signal,
    get_last_control_signal_scope,
    set_workflow_result_envelope,
)
from ..plan_state_runtime import (
    apply_workflow_step_checkpoint,
    compute_plan_state_progress,
    evaluate_workflow_completion_gate,
    mark_workflow_plan_state_entry,
    mark_workflow_plan_state_resume,
)
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
    result_envelope: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serialisable dict."""
        return {
            "instance_id": self.instance_id,
            "data": self.data,
            "completed": self.completed,
            "final_state": self.final_state,
            "error": self.error,
            "step_count": self.step_count,
            "result_envelope": self.result_envelope,
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
            mark_workflow_plan_state_resume(
                context=context,
                workflow_id=definition.workflow_id,
                definition_metadata=definition.metadata,
                current_state=current_state,
                retry_count=int(instance.retry_count),
            )
        else:
            context = dict(instance.inputs)
            current_state = definition.initial_state
            step_index = 0
        clear_control_signal_context(context)

        # Create execution environment
        from ...languagemodels.llm_interface import (
            get_active_model_name,
            get_llm_client,
        )
        from .registry_factory import _get_or_build_durable_mcp_gateway

        llm_client = get_llm_client(
            user_concept_id=instance.user_id,
            org_concept_id=instance.org_id,
        )
        environment = WorkflowEnvironment(
            llm_client=llm_client,
            gateway=_get_or_build_durable_mcp_gateway(),
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
        validation_mode = get_metadata_validation_mode()
        enforce_metadata_failures = metadata_validation_failures_are_enforced(
            validation_mode
        )

        transitions = 0

        def _build_result(
            *,
            completed: bool,
            final_state: str,
            error: str | None = None,
            checkpoint: bool = False,
            error_step: str | None = None,
        ) -> DurableWorkflowResult:
            result_envelope = build_workflow_result_envelope(
                workflow_id=definition.workflow_id,
                completed=completed,
                final_state=final_state,
                error=error,
                control_signal=get_last_control_signal(context),
                return_payload=context.get(WORKFLOW_RETURN_PAYLOAD_KEY),
                context=context,
                transition_count=transitions,
            )
            set_workflow_result_envelope(context=context, envelope=result_envelope)
            if checkpoint:
                progress_current_value, progress_total_value, progress_message_value = (
                    compute_plan_state_progress(
                        context=context,
                        fallback_current=step_index,
                        fallback_total=total_steps,
                        fallback_message=final_state,
                    )
                )
                self._instance_manager.checkpoint(
                    instance_id,
                    current_state=final_state,
                    workflow_data=context,
                    step_index=step_index,
                    error=error,
                    error_step=error_step,
                    progress_current=progress_current_value,
                    progress_total=progress_total_value,
                    progress_message=progress_message_value,
                )
            return DurableWorkflowResult(
                instance_id=instance_id,
                data=context,
                completed=completed,
                final_state=final_state,
                error=error,
                step_count=step_index,
                result_envelope=result_envelope,
            )

        def _complete_with_gate(final_state: str) -> DurableWorkflowResult:
            gate_ok, gate_result = evaluate_workflow_completion_gate(
                context=context,
                workflow_id=definition.workflow_id,
                definition_metadata=definition.metadata,
                final_state=final_state,
            )
            if not gate_ok:
                blocking_reasons = []
                if isinstance(gate_result, Mapping):
                    blocking_reasons = [
                        str(item).strip()
                        for item in gate_result.get("blocking_reason_codes") or []
                        if str(item).strip()
                    ]
                error = "workflow_completion_gate_unmet"
                if blocking_reasons:
                    error = f"{error}:{'|'.join(blocking_reasons)}"
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=final_state,
                    error=error,
                    checkpoint=True,
                )
            trace.finish_completed()
            return _build_result(
                completed=True,
                final_state=final_state,
                checkpoint=True,
            )

        while transitions < self._max_transitions:
            transitions += 1
            step_index += 1

            # Check for cancellation
            if self._instance_manager.is_cancelled(instance_id):
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="cancelled",
                    checkpoint=True,
                )

            # Extend lock if worker_id provided
            if worker_id:
                self._instance_manager.extend_lock(instance_id, worker_id)

            # Get current state spec
            state_spec = definition.states.get(current_state)
            if state_spec is None:
                error = f"unknown_state:{current_state}"
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )

            mark_workflow_plan_state_entry(
                context=context,
                workflow_id=definition.workflow_id,
                state_id=current_state,
                definition_metadata=definition.metadata,
                state_metadata=state_spec.metadata,
            )

            # Record state entry in trace
            trace.record_state_transition(
                current_state,
                current_state,
                verdict={"status": "enter"},
            )

            if validation_mode == METADATA_VALIDATION_MODE_OFF:
                off_probe = validate_state_metadata_pre_action(
                    state_id=current_state,
                    metadata=state_spec.metadata,
                    context=context,
                )
                if off_probe.applied:
                    pre_validation = skipped_metadata_validation(
                        state_id=current_state,
                        phase="pre_action",
                        reason="disabled_by_rollout_mode",
                        mode=validation_mode,
                    )
                else:
                    pre_validation = apply_metadata_validation_mode(
                        result=off_probe,
                        mode=validation_mode,
                    )
            else:
                pre_validation = apply_metadata_validation_mode(
                    result=validate_state_metadata_pre_action(
                        state_id=current_state,
                        metadata=state_spec.metadata,
                        context=context,
                    ),
                    mode=validation_mode,
                )
            append_metadata_validation_event(context=context, result=pre_validation)
            if pre_validation.applied:
                trace.record_state_transition(
                    current_state,
                    current_state,
                    verdict=pre_validation.to_trace_verdict(),
                )
            if not pre_validation.ok and enforce_metadata_failures:
                error = format_metadata_validation_error(pre_validation)
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )

            # Execute actions
            state_has_failure_route = state_has_on_failure_transition(state_spec)
            state_has_unknown_route = state_has_on_unknown_transition(state_spec)
            state_has_break_route = state_has_on_break_transition(state_spec)
            state_has_continue_route = state_has_on_continue_transition(state_spec)
            context_before_actions = dict(context)
            for action in state_spec.actions:
                context_before_action = dict(context)
                action_target_id = action.target_id
                resolved_inputs = resolve_action_inputs_from_context(
                    action_inputs=action.inputs,
                    context=context,
                )
                result = execute_workflow_step_invocation(
                    registry=self._registry,
                    action=action,
                    resolved_inputs=resolved_inputs,
                    context=context,
                    env=environment,
                    trace=trace,
                )

                trace.record_action(
                    action_id=action_target_id,
                    inputs=resolved_inputs,
                    outputs=result.outputs,
                    status=result.status,
                    error=result.error,
                    call_id=result.call_id,
                    duration_ms=result.duration_ms,
                )

                action_outcome = normalise_action_outcome(result.status)
                if (
                    action_outcome != WORKFLOW_ACTION_OUTCOME_FAILURE
                    and isinstance(result.outputs, Mapping)
                ):
                    context.update(result.outputs)
                    apply_tool_output_context_mappings(
                        context=context,
                        metadata=state_spec.metadata,
                        action_outputs=result.outputs,
                        state_id=current_state,
                        action_id=action_target_id,
                    )

                step_envelope = build_step_result_envelope(
                    workflow_id=definition.workflow_id,
                    state_id=current_state,
                    action_id=action_target_id,
                    action_status=result.status,
                    action_outcome=action_outcome,
                    action_error=result.error,
                    action_outputs=result.outputs if isinstance(result.outputs, Mapping) else {},
                    control_signal=get_last_control_signal(context),
                    control_signal_scope=get_last_control_signal_scope(context),
                    duration_ms=result.duration_ms,
                    context_before=context_before_action,
                    context_after=context,
                )
                append_step_result_envelope(context=context, envelope=step_envelope)
                if step_envelope["control_signal"] != "none":
                    event = {
                        "status": "control_signal",
                        "workflow_id": definition.workflow_id,
                        "state_id": current_state,
                        "action_id": action_target_id,
                        "control_signal": step_envelope["control_signal"],
                        "control_signal_scope": step_envelope.get(
                            "control_signal_scope"
                        ),
                    }
                    append_runtime_event(context=context, event=event)
                    trace.record_state_transition(
                        current_state,
                        current_state,
                        verdict=event,
                    )

                if action_outcome == WORKFLOW_ACTION_OUTCOME_FAILURE:
                    if state_has_failure_route:
                        # Safety envelope (JVNAUTOSCI-1087): if the state defines
                        # an explicit on_failure branch, do not fail-fast here.
                        # Keep failure markers in context and evaluate transitions.
                        break
                    error = result.error or "action_failed"
                    # Checkpoint the failure state
                    trace.finish_failed(error)
                    return _build_result(
                        completed=False,
                        final_state=current_state,
                        error=error,
                        checkpoint=True,
                        error_step=action_target_id,
                    )
                if action_outcome == WORKFLOW_ACTION_OUTCOME_UNKNOWN:
                    if state_has_unknown_route:
                        # Unknown outcomes require explicit routing; do not
                        # silently continue via default transitions.
                        break
                    error = result.error or "action_unknown"
                    trace.finish_failed(error)
                    return _build_result(
                        completed=False,
                        final_state=current_state,
                        error=error,
                        checkpoint=True,
                        error_step=action_target_id,
                    )

                control_signal = get_last_control_signal(context)
                if control_signal in {
                    WORKFLOW_CONTROL_SIGNAL_BREAK,
                    WORKFLOW_CONTROL_SIGNAL_CONTINUE,
                    WORKFLOW_CONTROL_SIGNAL_RETURN,
                }:
                    break

            control_signal = get_last_control_signal(context)
            if control_signal == WORKFLOW_CONTROL_SIGNAL_BREAK and not state_has_break_route:
                trace.finish_failed("workflow_break_outside_loop_scope")
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="workflow_break_outside_loop_scope",
                    checkpoint=True,
                )
            if (
                control_signal == WORKFLOW_CONTROL_SIGNAL_CONTINUE
                and not state_has_continue_route
            ):
                trace.finish_failed("workflow_continue_outside_loop_scope")
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="workflow_continue_outside_loop_scope",
                    checkpoint=True,
                )

            if current_state in termination_states:
                materialise_terminal_effect_context(
                    context=context,
                    state_spec=state_spec,
                    state_id=current_state,
                )

            if state_has_unknown_route and bool(context.get("last_action_unknown")):
                post_validation = skipped_metadata_validation(
                    state_id=current_state,
                    phase="post_action",
                    reason="action_unknown_with_on_unknown_route",
                    mode=validation_mode,
                )
            elif state_has_failure_route and bool(context.get("last_action_failed")):
                post_validation = skipped_metadata_validation(
                    state_id=current_state,
                    phase="post_action",
                    reason="action_failed_with_on_failure_route",
                    mode=validation_mode,
                )
            elif validation_mode == METADATA_VALIDATION_MODE_OFF:
                off_probe = validate_state_metadata_post_action(
                    state_id=current_state,
                    metadata=state_spec.metadata,
                    context_before=context_before_actions,
                    context_after=context,
                )
                if off_probe.applied:
                    post_validation = skipped_metadata_validation(
                        state_id=current_state,
                        phase="post_action",
                        reason="disabled_by_rollout_mode",
                        mode=validation_mode,
                    )
                else:
                    post_validation = apply_metadata_validation_mode(
                        result=off_probe,
                        mode=validation_mode,
                    )
            else:
                post_validation = apply_metadata_validation_mode(
                    result=validate_state_metadata_post_action(
                        state_id=current_state,
                        metadata=state_spec.metadata,
                        context_before=context_before_actions,
                        context_after=context,
                    ),
                    mode=validation_mode,
                )

            append_metadata_validation_event(context=context, result=post_validation)
            if post_validation.applied:
                trace.record_state_transition(
                    current_state,
                    current_state,
                    verdict=post_validation.to_trace_verdict(),
                )
            if not post_validation.ok and enforce_metadata_failures:
                error = format_metadata_validation_error(post_validation)
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )

            apply_workflow_step_checkpoint(
                context=context,
                workflow_id=definition.workflow_id,
                state_id=current_state,
                step_index=step_index,
                definition_metadata=definition.metadata,
                state_metadata=state_spec.metadata,
                blocked=False,
            )

            if control_signal == WORKFLOW_CONTROL_SIGNAL_RETURN:
                return _complete_with_gate(current_state)

            # Check for terminal state
            if current_state in termination_states:
                return _complete_with_gate(current_state)

            # Evaluate transitions
            next_state = None
            transition_reason = None
            for transition in state_spec.transitions:
                try:
                    condition_result = bool(transition.condition(context))
                    if (
                        not condition_result
                        and isinstance(transition.condition_spec, Mapping)
                    ):
                        condition_result = evaluate_transition_condition_spec(
                            context=context,
                            condition_spec=transition.condition_spec,
                        )
                    if condition_result:
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
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )

            # CHECKPOINT after successful step (before transitioning)
            progress_current_value, progress_total_value, progress_message_value = (
                compute_plan_state_progress(
                    context=context,
                    fallback_current=step_index,
                    fallback_total=total_steps,
                    fallback_message=next_state,
                )
            )
            self._instance_manager.checkpoint(
                instance_id,
                current_state=next_state,
                workflow_data=context,
                step_index=step_index,
                progress_current=progress_current_value,
                progress_total=progress_total_value,
                progress_message=progress_message_value,
            )

            trace.record_state_transition(
                current_state,
                next_state,
                reason=transition_reason,
            )
            current_state = next_state
            clear_control_signal_context(context)

        # Transition limit exceeded
        error = "transition_limit"
        trace.finish_failed(error)
        return _build_result(
            completed=False,
            final_state=current_state,
            error=error,
            checkpoint=True,
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
