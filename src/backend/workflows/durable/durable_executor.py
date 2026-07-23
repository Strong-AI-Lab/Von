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
from ...services.relationship_extent_index_service import (
    defer_relationship_extent_index_sync,
)
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
    LAST_WORKFLOW_CHECKPOINT_PAUSE_EVENT_KEY,
    WORKFLOW_CONTROL_SIGNAL_RETURN,
    WORKFLOW_CHECKPOINT_PAUSE_EVENTS_KEY,
    WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY,
    WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY,
    WORKFLOW_CHECKPOINT_PAUSE_REQUEST_SCHEMA_VERSION,
    WORKFLOW_CHECKPOINT_RESUME_RECEIPT_KEY,
    WORKFLOW_RETURN_PAYLOAD_KEY,
    build_workflow_result_envelope,
    clear_control_signal_context,
    get_last_control_signal,
    set_workflow_result_envelope,
    workflow_final_state_is_failure_like,
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
    CHECKPOINT_CONTEXT_PROJECTION_SCHEMA_VERSION,
    project_workflow_context_for_checkpoint,
)
from .authority_snapshot_attestation import (
    DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY,
    PROMPT_CONTEXT_DIAGNOSTICS_KEY,
    RESERVED_AUTHORITY_CONTEXT_KEYS,
    WORKFLOW_AUTHORITY_OUTPUT_KEY,
    build_authority_checkpoint_attestation,
    validate_authority_checkpoint_attestation,
    worker_claim_supports_exact_authority_snapshot,
)
from .instance_manager import WorkflowInstanceManager

logger = logging.getLogger(__name__)


_DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_FIELDS = (
    "schema_version",
    "version",
    "workflow_id",
    "source",
    "definition_hash",
    "runtime_definition_hash",
    "authoritative_definition_hash",
    "hash_mismatch",
    "state_count",
    "action_count",
)


def _execution_required_checkpoint_context_keys(
    definition: WorkflowDefinition,
) -> tuple[str, ...]:
    """Return top-level context roots declared as workflow reads.

    Durable checkpoints may compact diagnostic data, but represented values
    that a state can read must survive a worker restart without truncation.
    Dotted reads retain their top-level container; the instance payload store
    then offloads large private values to the namespace-scoped blob store.
    """

    required: set[str] = set()
    for state in definition.states.values():
        metadata = state.metadata if isinstance(state.metadata, Mapping) else {}
        for metadata_key in (
            "preconditions",
            "reads_variables",
            "reads_context_keys",
        ):
            values = metadata.get(metadata_key)
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, (list, tuple, set, frozenset)):
                continue
            for value in values:
                text = str(value or "").strip()
                if not text:
                    continue
                root = text.split(".", 1)[0].strip()
                if root:
                    required.add(root)
    return tuple(sorted(required))


def _definition_has_state_cycle(definition: WorkflowDefinition) -> bool:
    """Return whether the represented state graph can revisit a state."""

    adjacency = {
        state_id: tuple(
            transition.to_state
            for transition in state.transitions
            if transition.to_state in definition.states
        )
        for state_id, state in definition.states.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(state_id: str) -> bool:
        if state_id in visiting:
            return True
        if state_id in visited:
            return False
        visiting.add(state_id)
        if any(_visit(next_state) for next_state in adjacency.get(state_id, ())):
            return True
        visiting.remove(state_id)
        visited.add(state_id)
        return False

    return any(_visit(state_id) for state_id in adjacency)


def _mapping_context_key(value: Mapping[str, Any]) -> str:
    raw = str(
        value.get("context_key")
        or value.get("target_context_key_concept_id")
        or value.get("context_key_concept_id")
        or value.get("workflow_context_key")
        or value.get("target_context_key")
        or ""
    ).strip()
    if raw.startswith("#V#workflow_context_key_"):
        return raw[len("#V#workflow_context_key_") :]
    if raw.startswith("workflow_context_key_"):
        return raw[len("workflow_context_key_") :]
    if raw.startswith("#V#"):
        return raw[3:]
    return raw


def _definition_exact_snapshot_dependency_ineligibility_reasons(
    definition: WorkflowDefinition,
) -> set[str]:
    """Return unbound execution surfaces that prevent an exact output claim.

    An exact durable authority snapshot currently attests the parent workflow
    definition and worker claim only.  It does not bind child-workflow
    definitions, MCP/tool implementations, or arbitrary registry action
    implementations.  Fail closed for those surfaces until their producer
    identity can be carried into the checkpoint attestation.  Exact eligibility
    therefore requires exactly one self-contained ``llm.action`` whose
    represented policy explicitly disables tool use.  More than one cannot be
    bound reliably to the workflow-level authority output and last prompt
    diagnostics.
    """

    reasons: set[str] = set()
    self_contained_llm_producer_count = 0
    authority_output_mapping_count = 0
    for state in definition.states.values():
        if state.metadata.get("idempotency_policy") is not None:
            reasons.add("authority_producer_idempotency_surface")
        if state.metadata.get("retry_policy") is not None:
            reasons.add("authority_producer_retry_surface")
        raw_mappings = state.metadata.get("tool_output_context_mappings")
        mappings = (
            [raw_mappings]
            if isinstance(raw_mappings, Mapping)
            else raw_mappings
            if isinstance(raw_mappings, list)
            else []
        )
        for mapping in mappings:
            if not isinstance(mapping, Mapping):
                continue
            target_key = _mapping_context_key(mapping)
            if target_key == WORKFLOW_AUTHORITY_OUTPUT_KEY:
                authority_output_mapping_count += 1
            elif target_key in {
                PROMPT_CONTEXT_DIAGNOSTICS_KEY,
                DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY,
            }:
                reasons.add("authority_lineage_mapping_override")
        for action in state.actions:
            action_id = str(action.action_id or "").strip()
            if (
                action.is_subworkflow_step
                or bool(str(action.subworkflow_id or "").strip())
                or action_id == "workflow_invoke_subworkflow"
            ):
                reasons.add("unbound_child_execution_surface")
                continue

            if action.is_llm_step and action_id == "llm.action":
                llm_policy = (
                    action.llm_policy
                    if isinstance(action.llm_policy, Mapping)
                    else {}
                )
                tool_mode = str(llm_policy.get("tool_mode") or "").strip().lower()
                if tool_mode == "none":
                    self_contained_llm_producer_count += 1
                    continue
                reasons.add("unbound_tool_execution_surface")
                continue

            # Deterministic/control registry actions, including
            # workflow_mcp.invoke_tool, are executable code or dynamic
            # dependency surfaces whose implementation identity is not yet
            # included in the exact-snapshot attestation.
            reasons.add("unbound_action_execution_surface")
    if self_contained_llm_producer_count == 0 and not reasons:
        reasons.add("authority_output_producer_action_missing")
    elif self_contained_llm_producer_count > 1:
        # The bridge binds the top-level authority output to the workflow's
        # prompt diagnostics.  With multiple LLM steps, definition shape alone
        # cannot prove which prompt produced that output (or whether a later
        # step merely overwrote it), so no exact producer claim is possible.
        reasons.add("ambiguous_authority_output_producer")
    if _definition_has_state_cycle(definition):
        reasons.add("authority_producer_cyclic_execution_surface")
    if authority_output_mapping_count > 1:
        reasons.add("ambiguous_authority_output_mapping")
    return reasons


def _discard_reserved_authority_context(context: dict[str, Any]) -> None:
    for key in RESERVED_AUTHORITY_CONTEXT_KEYS:
        context.pop(key, None)


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _project_executed_workflow_definition_identity(
    value: Mapping[str, Any] | None,
    *,
    workflow_id: str,
) -> dict[str, Any] | None:
    """Return a bounded, internally consistent v1 identity for persistence."""

    if not isinstance(value, Mapping):
        return None
    source = str(value.get("source") or "").strip()
    definition_hash = value.get("definition_hash")
    runtime_hash = value.get("runtime_definition_hash")
    authoritative_hash = value.get("authoritative_definition_hash")
    if (
        value.get("schema_version") != "workflow_definition_identity.v1"
        or value.get("version") != 1
        or value.get("workflow_id") != workflow_id
        or not source
        or not _is_sha256(definition_hash)
        or not _is_sha256(runtime_hash)
        or runtime_hash != definition_hash
        or value.get("hash_mismatch") is not False
        or not isinstance(value.get("state_count"), int)
        or isinstance(value.get("state_count"), bool)
        or value.get("state_count", -1) < 0
        or not isinstance(value.get("action_count"), int)
        or isinstance(value.get("action_count"), bool)
        or value.get("action_count", -1) < 0
    ):
        return None
    if source.lower() == "vontology":
        if authoritative_hash != definition_hash or not _is_sha256(
            authoritative_hash
        ):
            return None
    elif authoritative_hash is not None and (
        not _is_sha256(authoritative_hash)
        or authoritative_hash != definition_hash
    ):
        return None

    return {
        key: value.get(key)
        for key in _DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_FIELDS
    }


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
            include_registry=True,
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

    @defer_relationship_extent_index_sync()
    def run_durable(
        self,
        instance_id: str,
        definition: WorkflowDefinition,
        *,
        worker_id: str | None = None,
        claim_token: str | None = None,
        resume_from_checkpoint: bool = True,
        workflow_definition_identity: Mapping[str, Any] | None = None,
    ) -> DurableWorkflowResult:
        """Execute a workflow with checkpointing.

        Args:
            instance_id: The workflow instance ID.
            definition: The workflow definition to execute.
            worker_id: Optional worker ID for lock extension.
            claim_token: Opaque token for fencing one concrete worker claim.
            resume_from_checkpoint: Whether to resume from saved state.
            workflow_definition_identity: Identity paired with the definition
                loaded by the current worker. Untrusted launch/checkpoint values
                under the persisted identity key are always discarded.

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
            lambda: self._instance_manager.get_instance(
                instance_id,
                for_execution=True,
            ),
        )
        if instance is None:
            return DurableWorkflowResult(
                instance_id=instance_id,
                data={},
                completed=False,
                final_state="",
                error="instance_not_found",
            )

        fenced_execution_requested = worker_id is not None or claim_token is not None
        if fenced_execution_requested and (
            not worker_id
            or not claim_token
            or instance.locked_by != worker_id
            or instance.claim_token != claim_token
        ):
            return DurableWorkflowResult(
                instance_id=instance_id,
                data={},
                completed=False,
                final_state=instance.current_state,
                error="durable_lock_lost",
                step_count=instance.step_index,
            )

        # Restore or initialise state
        resuming_persisted_checkpoint = bool(
            resume_from_checkpoint and instance.workflow_data
        )
        prior_attestation: dict[str, Any] | None = None
        prior_attestation_error: str | None = None
        if resuming_persisted_checkpoint:
            prior_attestation, prior_attestation_error = (
                validate_authority_checkpoint_attestation(
                    instance.authority_checkpoint_attestation,
                    instance_id=instance_id,
                    workflow_id=definition.workflow_id,
                    current_state=instance.current_state or definition.initial_state,
                    step_index=instance.step_index,
                    workflow_data=instance.workflow_data,
                )
            )
        if resuming_persisted_checkpoint:
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
        persisted_checkpoint_state = instance.current_state
        persisted_checkpoint_step_index = instance.step_index
        lifecycle_context_keys = (
            WORKFLOW_CHECKPOINT_PAUSE_EVENTS_KEY,
            LAST_WORKFLOW_CHECKPOINT_PAUSE_EVENT_KEY,
            WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY,
            WORKFLOW_CHECKPOINT_RESUME_RECEIPT_KEY,
        )
        if not resuming_persisted_checkpoint:
            for lifecycle_key in lifecycle_context_keys:
                context.pop(lifecycle_key, None)
        else:
            # These receipts come from manager-side compare-and-swap state,
            # never from workflow inputs or action-authored context.
            if isinstance(instance.checkpoint_pause_receipt, Mapping):
                context[WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY] = dict(
                    instance.checkpoint_pause_receipt
                )
            if isinstance(instance.checkpoint_resume_receipt, Mapping):
                context[WORKFLOW_CHECKPOINT_RESUME_RECEIPT_KEY] = dict(
                    instance.checkpoint_resume_receipt
                )
        # Persisted instance actor scope is execution authority. Neither launch
        # inputs nor a restored checkpoint may replace it. The executed
        # definition identity and authority outputs have the same trust boundary.
        # Launch inputs never own them. A resumed value is retained only when a
        # manager-side checkpoint attestation binds it to the preceding claim.
        context["user_concept_id"] = instance.user_id
        context["org_concept_id"] = instance.org_id
        context["organisation_concept_id"] = instance.org_id
        context["namespace"] = instance.namespace
        context["user_namespace"] = instance.namespace
        executed_definition_identity = (
            _project_executed_workflow_definition_identity(
                workflow_definition_identity,
                workflow_id=definition.workflow_id,
            )
        )
        exact_snapshot_ineligibility_reasons: set[str] = set()
        if executed_definition_identity is None:
            exact_snapshot_ineligibility_reasons.add(
                "executed_definition_identity_unverified"
            )
        if not worker_claim_supports_exact_authority_snapshot(
            instance.claimed_by_build,
            expected_worker_id=worker_id,
        ):
            exact_snapshot_ineligibility_reasons.add(
                "worker_exact_snapshot_capability_missing"
            )
        exact_snapshot_ineligibility_reasons.update(
            _definition_exact_snapshot_dependency_ineligibility_reasons(definition)
        )

        retain_resumed_authority = False
        if resuming_persisted_checkpoint:
            if prior_attestation is None:
                exact_snapshot_ineligibility_reasons.add(
                    prior_attestation_error
                    or "prior_checkpoint_attestation_unverified"
                )
            else:
                if not prior_attestation.get("exact_snapshot_eligible"):
                    exact_snapshot_ineligibility_reasons.update(
                        str(reason)
                        for reason in prior_attestation.get(
                            "ineligibility_reasons", []
                        )
                        if str(reason).strip()
                    )
                prior_definition_hash = prior_attestation.get(
                    "executed_definition_hash"
                )
                current_definition_hash = (
                    executed_definition_identity.get("definition_hash")
                    if executed_definition_identity is not None
                    else None
                )
                if prior_definition_hash != current_definition_hash:
                    exact_snapshot_ineligibility_reasons.add(
                        "workflow_definition_drift_on_resume"
                    )
                else:
                    retain_resumed_authority = True
                prior_authority_was_produced = any(
                    instance.workflow_data.get(key) is not None
                    for key in (
                        WORKFLOW_AUTHORITY_OUTPUT_KEY,
                        PROMPT_CONTEXT_DIAGNOSTICS_KEY,
                    )
                )
                if prior_authority_was_produced:
                    # The v1 attestation binds a checkpoint, not a producer
                    # invocation chain.  Retain same-claim data for ordinary
                    # workflow continuity, but do not call a resumed authority
                    # value exact until that producer chain is represented.
                    exact_snapshot_ineligibility_reasons.add(
                        "authority_output_resumed_without_producer_lineage"
                    )
                if prior_authority_was_produced and (
                    prior_attestation.get("claim_token") != claim_token
                    or prior_attestation.get("worker_id") != worker_id
                ):
                    # Do not let a successor claim re-attest output produced by
                    # its predecessor.  The ineligibility reason is carried into
                    # the next manager-side attestation, so A -> B -> A claim
                    # churn cannot make the old output exact again.
                    exact_snapshot_ineligibility_reasons.add(
                        "authority_producer_claim_changed_on_resume"
                    )
                    retain_resumed_authority = False
        if not retain_resumed_authority:
            _discard_reserved_authority_context(context)
        else:
            context.pop(DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY, None)

        def _reassert_executed_definition_identity() -> None:
            # Workflow actions share the mutable context and must not be able to
            # forge or delete this executor-owned persistence fact.
            context.pop(DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY, None)
            if executed_definition_identity is not None:
                context[DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY] = dict(
                    executed_definition_identity
                )

        _reassert_executed_definition_identity()
        clear_control_signal_context(context)
        # Pause requests are executor-owned, single-use control values.  A
        # launch input or restored checkpoint must not be able to forge a new
        # pause acknowledgement.
        context.pop(WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY, None)

        # Create execution environment
        from ...languagemodels.llm_interface import (
            get_active_model_parameters,
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
        effective_model_parameters = requested_model_parameters or (
            get_active_model_parameters(
                user_concept_id=instance.user_id,
                org_concept_id=instance.org_id,
            )
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
            model_parameters=effective_model_parameters or None,
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
        trace.metadata["durable_claim_fenced"] = bool(
            worker_id is not None and claim_token is not None
        )
        if isinstance(instance.checkpoint_pause_receipt, Mapping):
            trace.metadata[WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY] = dict(
                instance.checkpoint_pause_receipt
            )
        if isinstance(instance.checkpoint_resume_receipt, Mapping):
            trace.metadata[WORKFLOW_CHECKPOINT_RESUME_RECEIPT_KEY] = dict(
                instance.checkpoint_resume_receipt
            )
        if executed_definition_identity is not None:
            trace.metadata[DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY] = dict(
                executed_definition_identity
            )
        def _sync_exact_snapshot_trace_metadata() -> None:
            trace.metadata["exact_authority_snapshot_eligible"] = not bool(
                exact_snapshot_ineligibility_reasons
            )
            if exact_snapshot_ineligibility_reasons:
                trace.metadata[
                    "exact_authority_snapshot_ineligibility_reasons"
                ] = sorted(exact_snapshot_ineligibility_reasons)
            else:
                trace.metadata.pop(
                    "exact_authority_snapshot_ineligibility_reasons",
                    None,
                )

        _sync_exact_snapshot_trace_metadata()
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
            if effective_model_parameters:
                trace.metadata["effective_model_parameters"] = dict(
                    effective_model_parameters
                )
                trace.metadata["effective_model_parameters_source"] = (
                    "requested" if requested_model_parameters else "active_setting"
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

        def _checkpoint_payload(
            *,
            checkpoint_state: str,
            checkpoint_step_index: int,
        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
            _reassert_executed_definition_identity()
            if any(
                context.get(key) is not None
                for key in (
                    WORKFLOW_AUTHORITY_OUTPUT_KEY,
                    PROMPT_CONTEXT_DIAGNOSTICS_KEY,
                )
            ):
                producer_action_records = [
                    action_record
                    for action_record in trace.actions
                    if str(action_record.get("action_id") or "").strip()
                    == "llm.action"
                ]
                observed_producer_invocations = len(producer_action_records)
                if observed_producer_invocations == 0:
                    exact_snapshot_ineligibility_reasons.add(
                        "authority_producer_invocation_missing"
                    )
                elif observed_producer_invocations > 1:
                    exact_snapshot_ineligibility_reasons.add(
                        "ambiguous_authority_output_producer_invocations"
                    )
                if observed_producer_invocations == 1:
                    producer_outputs = producer_action_records[0].get("outputs")
                    producer_diagnostics = (
                        producer_outputs.get(PROMPT_CONTEXT_DIAGNOSTICS_KEY)
                        if isinstance(producer_outputs, Mapping)
                        else None
                    )
                    if isinstance(producer_diagnostics, Mapping):
                        # Reassert prompt lineage from the actual LLM action
                        # record after all represented output mappings ran.
                        # The definition may map validated JSON to the authority
                        # output, but it may not author its own execution lineage.
                        context[PROMPT_CONTEXT_DIAGNOSTICS_KEY] = dict(
                            producer_diagnostics
                        )
                    else:
                        context.pop(PROMPT_CONTEXT_DIAGNOSTICS_KEY, None)
                        exact_snapshot_ineligibility_reasons.add(
                            "authority_producer_prompt_lineage_missing"
                        )
            projected = project_workflow_context_for_checkpoint(
                context,
                lossless_keys=_execution_required_checkpoint_context_keys(definition),
            )
            projection = projected.get(CHECKPOINT_CONTEXT_PROJECTION_KEY)
            projected_key_records = (
                projection.get("projected_keys")
                if isinstance(projection, Mapping)
                else None
            )
            if (
                not isinstance(projection, Mapping)
                or projection.get("schema_version")
                != CHECKPOINT_CONTEXT_PROJECTION_SCHEMA_VERSION
                or not isinstance(projected_key_records, list)
            ):
                exact_snapshot_ineligibility_reasons.add(
                    "authority_checkpoint_projection_unverified"
                )
            elif int(projection.get("omitted_projected_key_count") or 0) > 0:
                exact_snapshot_ineligibility_reasons.add(
                    "authority_checkpoint_projection_unverified"
                )
            if isinstance(projected_key_records, list) and any(
                isinstance(record, Mapping)
                and str(record.get("key") or "").strip()
                in RESERVED_AUTHORITY_CONTEXT_KEYS
                for record in projected_key_records
            ):
                exact_snapshot_ineligibility_reasons.add(
                    "authority_checkpoint_projection_lossy"
                )
            _sync_exact_snapshot_trace_metadata()
            attestation = None
            if (
                worker_id is not None
                and claim_token is not None
                and worker_claim_supports_exact_authority_snapshot(
                    instance.claimed_by_build,
                    expected_worker_id=worker_id,
                )
            ):
                attestation = build_authority_checkpoint_attestation(
                    instance_id=instance_id,
                    workflow_id=definition.workflow_id,
                    current_state=checkpoint_state,
                    step_index=checkpoint_step_index,
                    claim_token=claim_token,
                    worker_id=worker_id,
                    workflow_data=projected,
                    exact_snapshot_eligible=(
                        not exact_snapshot_ineligibility_reasons
                    ),
                    ineligibility_reasons=sorted(
                        exact_snapshot_ineligibility_reasons
                    ),
                )
            return projected, attestation

        def _build_result(
            *,
            completed: bool,
            final_state: str,
            error: str | None = None,
            checkpoint: bool = False,
            error_step: str | None = None,
        ) -> DurableWorkflowResult:
            nonlocal persisted_execution_trace_id
            _reassert_executed_definition_identity()
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
                checkpoint_data, checkpoint_attestation = _checkpoint_payload(
                    checkpoint_state=final_state,
                    checkpoint_step_index=step_index,
                )
                # Projection can make an otherwise static definition ineligible.
                # Persist the trace only after that verdict is known so the TER
                # and manager-side attestation cannot disagree.
                if persisted_execution_trace_id is None:
                    persisted_execution_trace_id = _retry_store_call(
                        "insert_workflow_execution_trace",
                        lambda: insert_workflow_execution_trace(
                            trace.to_storage_document()
                        ),
                    )
                checkpoint_saved = _retry_store_call(
                    "checkpoint_terminal",
                    lambda: self._instance_manager.checkpoint(
                        instance_id,
                        current_state=final_state,
                        workflow_data=checkpoint_data,
                        step_index=step_index,
                        error=error,
                        error_step=error_step,
                        progress_current=progress_current_value,
                        progress_total=progress_total_value,
                        progress_message=progress_message_value,
                        execution_trace_id=persisted_execution_trace_id,
                        worker_id=worker_id,
                        claim_token=claim_token,
                        authority_checkpoint_attestation=checkpoint_attestation,
                        workflow_id=definition.workflow_id,
                    ),
                )
                if fenced_execution_requested and checkpoint_saved is not True:
                    return DurableWorkflowResult(
                        instance_id=instance_id,
                        data={},
                        completed=False,
                        final_state=final_state,
                        error="durable_lock_lost",
                        step_count=step_index,
                        execution_trace_id=persisted_execution_trace_id,
                    )
            if persisted_execution_trace_id is None:
                persisted_execution_trace_id = _retry_store_call(
                    "insert_workflow_execution_trace",
                    lambda: insert_workflow_execution_trace(
                        trace.to_storage_document()
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
                    checkpoint=False,
                )

            if worker_id:
                try:
                    lock_extended = _retry_store_call(
                        "extend_lock",
                        lambda: self._instance_manager.extend_lock(
                            instance_id,
                            worker_id,
                            claim_token=claim_token,
                        ),
                    )
                except Exception:
                    logger.warning(
                        "[durable_workflow] Failed to extend lock for %s during execution",
                        instance_id,
                        exc_info=True,
                    )
                    lock_extended = False
                if lock_extended is not True:
                    trace.finish_failed("durable_lock_lost")
                    return _build_result(
                        completed=False,
                        final_state=current_state,
                        error="durable_lock_lost",
                        checkpoint=False,
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
            if workflow_final_state_is_failure_like(current_state):
                error = "workflow_failed_terminal_state"
                trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                    checkpoint=True,
                )
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
            raw_pause_request = context.pop(
                WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY,
                None,
            )
            pause_request: dict[str, Any] | None = None
            pause_event: dict[str, Any] | None = None
            if raw_pause_request is not None:
                if (
                    not isinstance(raw_pause_request, Mapping)
                    or raw_pause_request.get("schema_version")
                    != WORKFLOW_CHECKPOINT_PAUSE_REQUEST_SCHEMA_VERSION
                    or not str(
                        raw_pause_request.get("request_sha256") or ""
                    ).strip()
                ):
                    error = "workflow_checkpoint_pause_request_invalid"
                    trace.finish_failed(error)
                    return _build_result(
                        completed=False,
                        final_state=current_state,
                        error=error,
                        checkpoint=True,
                    )
                pause_request = dict(raw_pause_request)
                pause_event = {
                    "schema_version": "workflow_checkpoint_pause_event.v1",
                    "status": "requested",
                    "instance_id": instance_id,
                    "workflow_id": definition.workflow_id,
                    "request_state": current_state,
                    "checkpoint_state": resolved_next_state,
                    "checkpoint_step_index": step_index,
                    "reason_code": pause_request.get("reason_code"),
                    "request_sha256": pause_request.get("request_sha256"),
                }
                pause_events = context.get(WORKFLOW_CHECKPOINT_PAUSE_EVENTS_KEY)
                if not isinstance(pause_events, list):
                    pause_events = []
                    context[WORKFLOW_CHECKPOINT_PAUSE_EVENTS_KEY] = pause_events
                pause_events.append(pause_event)
                context[LAST_WORKFLOW_CHECKPOINT_PAUSE_EVENT_KEY] = pause_event
            progress_current_value, progress_total_value, progress_message_value = (
                compute_plan_state_progress(
                    context=context,
                    fallback_current=step_index,
                    fallback_total=total_steps,
                    fallback_message=resolved_next_state,
                )
            )
            checkpoint_data, checkpoint_attestation = _checkpoint_payload(
                checkpoint_state=resolved_next_state,
                checkpoint_step_index=step_index,
            )
            if pause_request is not None:
                if not worker_id or not claim_token or pause_event is None:
                    error = "workflow_checkpoint_pause_requires_durable_claim"
                    trace.finish_failed(error)
                    return _build_result(
                        completed=False,
                        final_state=current_state,
                        error=error,
                        checkpoint=True,
                    )

                trace.record_state_transition(
                    current_state,
                    resolved_next_state,
                    reason=transition_decision.reason,
                    verdict=pause_event,
                )
                trace.finish_paused()
                pause_receipt = _retry_store_call(
                    "pause_claim_at_checkpoint",
                    lambda: self._instance_manager.pause_claim_at_checkpoint(
                        instance_id,
                        prior_state=persisted_checkpoint_state,
                        prior_step_index=persisted_checkpoint_step_index,
                        request_state=current_state,
                        checkpoint_state=resolved_next_state,
                        checkpoint_step_index=step_index,
                        workflow_data=checkpoint_data,
                        pause_request=pause_request,
                        worker_id=worker_id,
                        claim_token=claim_token,
                        workflow_id=definition.workflow_id,
                        progress_current=progress_current_value,
                        progress_total=progress_total_value,
                        progress_message=progress_message_value,
                        execution_trace_id=trace.execution_id,
                        authority_checkpoint_attestation=checkpoint_attestation,
                    ),
                )
                if not isinstance(pause_receipt, Mapping):
                    trace.finish_failed("durable_lock_lost")
                    return _build_result(
                        completed=False,
                        final_state=current_state,
                        error="durable_lock_lost",
                        checkpoint=False,
                    )

                pause_receipt_dict = dict(pause_receipt)
                pause_event["status"] = "paused"
                pause_event["receipt"] = pause_receipt_dict
                context[WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY] = pause_receipt_dict
                trace.metadata[WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY] = (
                    pause_receipt_dict
                )
                current_state = resolved_next_state
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="paused_at_checkpoint",
                    checkpoint=False,
                )

            checkpoint_saved = _retry_store_call(
                "checkpoint_transition",
                lambda: self._instance_manager.checkpoint(
                    instance_id,
                    current_state=resolved_next_state,
                    workflow_data=checkpoint_data,
                    step_index=step_index,
                    progress_current=progress_current_value,
                    progress_total=progress_total_value,
                    progress_message=progress_message_value,
                    worker_id=worker_id,
                    claim_token=claim_token,
                    authority_checkpoint_attestation=checkpoint_attestation,
                    workflow_id=definition.workflow_id,
                ),
            )
            if fenced_execution_requested and checkpoint_saved is not True:
                trace.finish_failed("durable_lock_lost")
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="durable_lock_lost",
                    checkpoint=False,
                )
            if checkpoint_saved is True:
                persisted_checkpoint_state = resolved_next_state
                persisted_checkpoint_step_index = step_index

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
            # This helper executes synchronously without claiming the pending
            # row. Keep it on the explicit direct/admin lane; background workers
            # must use find_and_claim_instance and a claim token.
            worker_id=None,
            resume_from_checkpoint=False,
        )
