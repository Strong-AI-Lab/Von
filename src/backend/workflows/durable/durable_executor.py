"""Durable workflow executor with checkpoint support.

Extends the base WorkflowExecutor to persist state after each step,
enabling resume from the last checkpoint after interruption.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from ...db.transient_errors import run_with_transient_mongo_retry
from ..engine import (
    WorkflowDefinition,
    materialise_terminal_effect_context,
    WorkflowExecutor,
)
from ..metadata_validation import (
    format_metadata_validation_error,
)
from ..action_registry import (
    ActionRegistry,
    WorkflowEnvironment,
)
from ..trace_model import WorkflowExecutionTrace
from ..execution_contracts import (
    WORKFLOW_CONTROL_SIGNAL_RETURN,
    WORKFLOW_RETURN_PAYLOAD_KEY,
    build_workflow_result_envelope,
    clear_control_signal_context,
    get_last_control_signal,
    set_workflow_result_envelope,
)
from ..plan_state_runtime import (
    apply_workflow_step_checkpoint,
    compute_plan_state_progress,
    evaluate_workflow_completion_gate,
    mark_workflow_plan_state_resume,
)
from ..trace_store import insert_workflow_execution_trace
from .checkpoint_context_projection import (
    CHECKPOINT_CONTEXT_PROJECTION_KEY,
    project_workflow_context_for_checkpoint,
)
from .instance_manager import WorkflowInstanceManager

logger = logging.getLogger(__name__)


def _coerce_non_empty_text(value: Any) -> str | None:
    text = str(value).strip() if isinstance(value, str) else ""
    return text or None


def _infer_client_type_from_model(model_name: str | None) -> str | None:
    model = _coerce_non_empty_text(model_name)
    if not model:
        return None
    lowered = model.lower()
    if lowered.startswith("openai:") or lowered.startswith(
        ("gpt-", "o1-", "text-", "davinci", "curie", "babbage", "ada")
    ):
        return "openai"
    if lowered.startswith("ollama:") or (
        ":" in lowered and not lowered.startswith("ft:")
    ):
        return "ollama"
    if lowered.startswith("gemini"):
        return "gemini"
    return None


def _normalise_requested_model_name(
    model_name: str | None,
    *,
    client_type: str | None,
) -> str | None:
    requested_model = _coerce_non_empty_text(model_name)
    if not requested_model:
        return None

    from ...languagemodels.llm_interface import (
        resolve_ollama_model_name,
        resolve_openai_model_name,
    )

    if client_type == "openai":
        return resolve_openai_model_name(requested_model) or requested_model
    if client_type == "ollama":
        return resolve_ollama_model_name(requested_model) or requested_model
    return requested_model


def _resolve_instance_runtime_model_context(
    *,
    context: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> tuple[str | None, str | None, dict[str, Any]]:
    requested_model = (
        _coerce_non_empty_text(context.get("requested_model"))
        or _coerce_non_empty_text(inputs.get("requested_model"))
        or _coerce_non_empty_text(context.get("model"))
        or _coerce_non_empty_text(inputs.get("model"))
    )
    requested_client_type = (
        _coerce_non_empty_text(context.get("requested_client_type"))
        or _coerce_non_empty_text(inputs.get("requested_client_type"))
        or _coerce_non_empty_text(context.get("explicit_client_type"))
        or _coerce_non_empty_text(inputs.get("explicit_client_type"))
    )
    inferred_client_type = requested_client_type or _infer_client_type_from_model(
        requested_model
    )
    normalised_requested_model = _normalise_requested_model_name(
        requested_model,
        client_type=inferred_client_type,
    )
    raw_parameters = (
        context.get("requested_model_parameters")
        or inputs.get("requested_model_parameters")
        or context.get("model_parameters")
        or inputs.get("model_parameters")
    )
    try:
        from ...services.model_parameter_service import (
            normalise_model_parameters_for_storage,
        )

        model_parameters = normalise_model_parameters_for_storage(
            raw_parameters,
            provider=inferred_client_type,
            model=normalised_requested_model,
        )
    except Exception:
        model_parameters = {}
    return normalised_requested_model, inferred_client_type, model_parameters


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
    execution_trace_id: str | None = None

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
            "execution_trace_id": self.execution_trace_id,
        }


class DurableWorkflowExecutor(WorkflowExecutor):
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
        super().__init__(registry=registry, max_transitions=max_transitions)
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
        def _retry_store_call(operation_name: str, operation):
            return run_with_transient_mongo_retry(
                operation,
                operation_name=f"durable_executor.{operation_name}:{instance_id}",
                logger_obj=logger,
            )

        # Load instance
        instance = _retry_store_call(
            "get_instance",
            lambda: self._instance_manager.get_instance(instance_id),
        )
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
            context.pop(CHECKPOINT_CONTEXT_PROJECTION_KEY, None)
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
        context.setdefault("user_concept_id", instance.user_id)
        context.setdefault("org_concept_id", instance.org_id)
        context.setdefault("namespace", instance.namespace)
        context.setdefault("user_namespace", instance.namespace)
        clear_control_signal_context(context)

        # Create execution environment
        from ...languagemodels.llm_interface import (
            get_active_model_name,
            get_llm_client,
            resolve_provider_from_model_concept,
        )
        from .registry_factory import _get_or_build_durable_mcp_gateway

        requested_model, requested_client_type, requested_model_parameters = (
            _resolve_instance_runtime_model_context(
                context=context,
                inputs=instance.inputs,
            )
        )
        effective_model = requested_model or get_active_model_name(
            user_concept_id=instance.user_id,
            org_concept_id=instance.org_id,
        )
        if requested_model:
            context.setdefault("requested_model", requested_model)
        if requested_client_type:
            context.setdefault("requested_client_type", requested_client_type)
        if requested_model_parameters:
            context.setdefault("requested_model_parameters", requested_model_parameters)

        llm_client = get_llm_client(
            client_type=requested_client_type,
            user_concept_id=instance.user_id,
            org_concept_id=instance.org_id,
        )
        environment = WorkflowEnvironment(
            llm_client=llm_client,
            gateway=_get_or_build_durable_mcp_gateway(),
            model=effective_model,
            model_parameters=requested_model_parameters or None,
            user_namespace=instance.namespace,
            user_concept_id=instance.user_id,
            org_concept_id=instance.org_id,
        )

        # Create trace for observability
        trace = WorkflowExecutionTrace(
            workflow_id=definition.workflow_id,
            execution_id=str(uuid.uuid4()),
            instance_id=instance_id,
            user_namespace=instance.namespace,
            org_id=instance.org_id,
        )
        if isinstance(environment.model, str) and environment.model.strip():
            default_model = environment.model.strip()
            trace.metadata["default_model"] = default_model
            if requested_model:
                trace.metadata["requested_model"] = requested_model
                trace.metadata["requested_model_override_applied"] = True
            if requested_client_type:
                trace.metadata["requested_client_type"] = requested_client_type
            if requested_model_parameters:
                trace.metadata["requested_model_parameters"] = dict(
                    requested_model_parameters
                )
            resolved_provider = resolve_provider_from_model_concept(default_model)
            if resolved_provider is None:
                lowered_model = default_model.lower()
                if lowered_model.startswith("openai:") or lowered_model.startswith(
                    ("gpt-", "o1-", "text-", "davinci", "curie", "babbage", "ada")
                ):
                    resolved_provider = "openai"
                elif lowered_model.startswith("ollama:") or (
                    ":" in lowered_model and not lowered_model.startswith("ft:")
                ):
                    resolved_provider = "ollama"
                elif lowered_model.startswith("gemini"):
                    resolved_provider = "gemini"
            if resolved_provider:
                trace.metadata["default_provider"] = resolved_provider
        persisted_execution_trace_id: str | None = None

        total_steps = max(1, len(definition.states))
        run_support = self._resolve_run_support(definition=definition)

        transitions = 0

        def _build_result(
            *,
            completed: bool,
            final_state: str,
            error: str | None = None,
            checkpoint: bool = False,
            error_step: str | None = None,
        ) -> DurableWorkflowResult:
            nonlocal persisted_execution_trace_id
            if persisted_execution_trace_id is None:
                persisted_execution_trace_id = _retry_store_call(
                    "insert_workflow_execution_trace",
                    lambda: insert_workflow_execution_trace(
                        trace.to_storage_document()
                    ),
                )
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
                _retry_store_call(
                    "checkpoint_terminal",
                    lambda: self._instance_manager.checkpoint(
                        instance_id,
                        current_state=final_state,
                        workflow_data=project_workflow_context_for_checkpoint(
                            context
                        ),
                        step_index=step_index,
                        error=error,
                        error_step=error_step,
                        progress_current=progress_current_value,
                        progress_total=progress_total_value,
                        progress_message=progress_message_value,
                        execution_trace_id=persisted_execution_trace_id,
                    ),
                )
            return DurableWorkflowResult(
                instance_id=instance_id,
                data=context,
                completed=completed,
                final_state=final_state,
                error=error,
                step_count=step_index,
                result_envelope=result_envelope,
                execution_trace_id=persisted_execution_trace_id,
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

            if _retry_store_call(
                "is_cancelled",
                lambda: self._instance_manager.is_cancelled(instance_id),
            ):
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="cancelled",
                    checkpoint=True,
                )

            if worker_id:
                try:
                    _retry_store_call(
                        "extend_lock",
                        lambda: self._instance_manager.extend_lock(
                            instance_id,
                            worker_id,
                        ),
                    )
                except Exception:
                    logger.warning(
                        "[durable_workflow] Failed to extend lock for %s during execution",
                        instance_id,
                        exc_info=True,
                    )

            state_spec = definition.states.get(current_state)
            if state_spec is None:
                error = f"unknown_state:{current_state}"
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )

            self._record_state_entry(
                definition=definition,
                state_spec=state_spec,
                state_id=current_state,
                context=context,
                trace=trace,
            )

            pre_validation = self._apply_pre_action_metadata_validation(
                state_id=current_state,
                state_metadata=state_spec.metadata,
                context=context,
                trace=trace,
                validation_mode=run_support.validation_mode,
            )
            if not pre_validation.ok and run_support.enforce_metadata_failures:
                error = format_metadata_validation_error(pre_validation)
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )

            try:
                state_support = self._resolve_state_runtime_support(
                    state_spec=state_spec
                )
            except ValueError as exc:
                error = str(exc)
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )

            context_before_actions = dict(context)
            try:
                approval_blocked = self._execute_state_actions(
                    definition=definition,
                    state_id=current_state,
                    state_spec=state_spec,
                    state_support=state_support,
                    context=context,
                    environment=environment,
                    trace=trace,
                )
            except ValueError as exc:
                error = str(exc)
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )
            except RuntimeError as exc:
                error = str(exc)
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                    error_step=getattr(exc, "action_id", None),
                )

            if current_state in run_support.termination_states:
                materialise_terminal_effect_context(
                    context=context,
                    state_spec=state_spec,
                    state_id=current_state,
                )

            control_signal_error = self._validate_control_signal_routing(
                state_support=state_support,
                context=context,
                approval_blocked=approval_blocked,
            )
            if control_signal_error is not None:
                trace.finish_failed(control_signal_error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=control_signal_error,
                    checkpoint=True,
                )

            post_validation = self._apply_post_action_metadata_validation(
                state_id=current_state,
                state_metadata=state_spec.metadata,
                state_support=state_support,
                context_before=context_before_actions,
                context=context,
                trace=trace,
                validation_mode=run_support.validation_mode,
                approval_blocked=approval_blocked,
            )
            if not post_validation.ok and run_support.enforce_metadata_failures:
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
                blocked=approval_blocked,
            )

            control_signal = get_last_control_signal(context)
            if control_signal == WORKFLOW_CONTROL_SIGNAL_RETURN:
                return _complete_with_gate(current_state)

            if current_state in run_support.termination_states:
                return _complete_with_gate(current_state)

            transition_decision = self._select_next_transition(
                state_spec=state_spec,
                context=context,
            )
            if transition_decision.next_state is None:
                error = "approval_required" if approval_blocked else "no_transition"
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )

            resolved_next_state = str(transition_decision.next_state)
            progress_current_value, progress_total_value, progress_message_value = (
                compute_plan_state_progress(
                    context=context,
                    fallback_current=step_index,
                    fallback_total=total_steps,
                    fallback_message=resolved_next_state,
                )
            )
            _retry_store_call(
                "checkpoint_transition",
                lambda: self._instance_manager.checkpoint(
                    instance_id,
                    current_state=resolved_next_state,
                    workflow_data=project_workflow_context_for_checkpoint(context),
                    step_index=step_index,
                    progress_current=progress_current_value,
                    progress_total=progress_total_value,
                    progress_message=progress_message_value,
                ),
            )

            trace.record_state_transition(
                current_state,
                resolved_next_state,
                reason=transition_decision.reason,
            )
            current_state = resolved_next_state
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
