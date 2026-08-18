"""Action handlers for first-class workflow control-flow primitives.

JVNAUTOSCI-1311:
- explicit break/continue step semantics for loop contexts,
- first-class fork/join semantics with deterministic merge and failure policy,
- shared runtime metrics/events for observability.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Mapping, Sequence

from ...services.kr_materialisation_guard_service import (
    validate_kr_materialisation_guard,
)
from ...services.kr_relationship_readback_service import (
    verify_kr_relationship_readback,
    verify_relationship_effect_readback,
)
from ...services.text_effect_readback_service import verify_text_effect_readback
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..context_paths import resolve_context_path
from ..engine import WorkflowDefinition, WorkflowExecutor
from ..execution_contracts import (
    LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
    WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY,
    WORKFLOW_CHECKPOINT_PAUSE_REQUEST_SCHEMA_VERSION,
    WORKFLOW_CONTROL_ACTION_BREAK_ID,
    WORKFLOW_CONTROL_ACTION_CONTEXT_PROJECT_ID,
    WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
    WORKFLOW_CONTROL_ACTION_CONTEXT_TEMPLATE_ID,
    WORKFLOW_CONTROL_ACTION_CONTINUE_ID,
    WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
    WORKFLOW_CONTROL_ACTION_FORK_ID,
    WORKFLOW_CONTROL_ACTION_JOIN_ID,
    WORKFLOW_CONTROL_ACTION_KR_MATERIALISATION_GUARD_ID,
    WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_READBACK_ID,
    WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_RESOLUTION_ID,
    WORKFLOW_CONTROL_ACTION_PAUSE_AT_CHECKPOINT_ID,
    WORKFLOW_CONTROL_ACTION_RELATIONSHIP_EFFECT_READBACK_ID,
    WORKFLOW_CONTROL_ACTION_TEXT_EFFECT_READBACK_ID,
    WORKFLOW_CONTROL_SIGNAL_BREAK,
    WORKFLOW_CONTROL_SIGNAL_CONTINUE,
    WORKFLOW_FOR_EACH_ALLOWED_SUCCESS_POLICIES,
    WORKFLOW_FOR_EACH_SUCCESS_POLICY_ALL_MUST_SUCCEED,
    WORKFLOW_FORK_ALLOWED_FAILURE_POLICIES,
    WORKFLOW_FORK_ALLOWED_MERGE_POLICIES,
    WORKFLOW_FORK_CONTEXTS_KEY,
    WORKFLOW_FORK_FAILURE_POLICY_COLLECT_ERRORS,
    WORKFLOW_FORK_FAILURE_POLICY_FAIL_FAST,
    WORKFLOW_FORK_MERGE_POLICY_LAST_WRITER_WINS,
    WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
    append_runtime_event,
    increment_runtime_metric,
)
from ..tool_invocation_evidence import (
    WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
    derive_tool_invocation_records_from_step_envelopes,
)
from ..trace_model import (
    WorkflowExecutionTrace,
    build_child_workflow_effect_identity_metadata,
)
from ..vontology_loader import load_workflow_definition_from_vontology
from .nested_workflow_authority import (
    NESTED_WORKFLOW_DEFINITION_NOT_FOUND,
    resolve_nested_workflow_definition,
)

_DEFAULT_FORK_BRANCH_LIMIT = 8
_MAX_FORK_BRANCH_LIMIT = 32
_DEFAULT_BRANCH_MAX_TRANSITIONS = 40
_MAX_BRANCH_MAX_TRANSITIONS = 200
_DEFAULT_FOR_EACH_ITEM_LIMIT = 16
_MAX_FOR_EACH_ITEM_LIMIT = 256
_DEFAULT_FOR_EACH_MAX_CONCURRENCY = 1
_MAX_FOR_EACH_MAX_CONCURRENCY = 16
_MAX_KR_RELATIONSHIP_RESOLUTION_CONCEPT_RESULTS = 24
_MAX_KR_RELATIONSHIP_RESOLUTION_SPECS = 40
_FORK_BRANCH_LIMIT_ENV = "VON_WORKFLOW_FORK_MAX_BRANCHES"
_FORK_BRANCH_TRANSITIONS_ENV = "VON_WORKFLOW_FORK_BRANCH_MAX_TRANSITIONS"
_FOR_EACH_ITEM_LIMIT_ENV = "VON_WORKFLOW_FOR_EACH_MAX_ITEMS"
_FOR_EACH_MAX_CONCURRENCY_ENV = "VON_WORKFLOW_FOR_EACH_MAX_CONCURRENCY"


def _normalise_text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_int(value: Any, *, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(min_value, min(max_value, parsed))


def _coerce_branch_limit() -> int:
    return _coerce_int(
        os.getenv(_FORK_BRANCH_LIMIT_ENV),
        default=_DEFAULT_FORK_BRANCH_LIMIT,
        min_value=1,
        max_value=_MAX_FORK_BRANCH_LIMIT,
    )


def _coerce_for_each_limit(value: Any) -> int:
    default_limit = _coerce_int(
        os.getenv(_FOR_EACH_ITEM_LIMIT_ENV),
        default=_DEFAULT_FOR_EACH_ITEM_LIMIT,
        min_value=1,
        max_value=_MAX_FOR_EACH_ITEM_LIMIT,
    )
    return _coerce_int(
        value,
        default=default_limit,
        min_value=1,
        max_value=_MAX_FOR_EACH_ITEM_LIMIT,
    )


def _coerce_for_each_max_concurrency(value: Any) -> int:
    default_concurrency = _coerce_int(
        os.getenv(_FOR_EACH_MAX_CONCURRENCY_ENV),
        default=_DEFAULT_FOR_EACH_MAX_CONCURRENCY,
        min_value=1,
        max_value=_MAX_FOR_EACH_MAX_CONCURRENCY,
    )
    return _coerce_int(
        value,
        default=default_concurrency,
        min_value=1,
        max_value=_MAX_FOR_EACH_MAX_CONCURRENCY,
    )


def _coerce_branch_max_transitions(value: Any) -> int:
    default_value = _coerce_int(
        os.getenv(_FORK_BRANCH_TRANSITIONS_ENV),
        default=_DEFAULT_BRANCH_MAX_TRANSITIONS,
        min_value=5,
        max_value=_MAX_BRANCH_MAX_TRANSITIONS,
    )
    return _coerce_int(
        value,
        default=default_value,
        min_value=5,
        max_value=_MAX_BRANCH_MAX_TRANSITIONS,
    )


def _coerce_bool_with_default(value: Any, *, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _normalise_failure_policy(value: Any) -> str:
    text = _normalise_text(value).lower()
    if text in WORKFLOW_FORK_ALLOWED_FAILURE_POLICIES:
        return text
    return WORKFLOW_FORK_FAILURE_POLICY_FAIL_FAST


def _normalise_merge_policy(value: Any) -> str:
    text = _normalise_text(value).lower()
    if text in WORKFLOW_FORK_ALLOWED_MERGE_POLICIES:
        return text
    return WORKFLOW_FORK_MERGE_POLICY_LAST_WRITER_WINS


def _normalise_for_each_success_policy(value: Any) -> str:
    text = _normalise_text(value).lower()
    if text in WORKFLOW_FOR_EACH_ALLOWED_SUCCESS_POLICIES:
        return text
    return WORKFLOW_FOR_EACH_SUCCESS_POLICY_ALL_MUST_SUCCEED


def _normalise_branch_specs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    specs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        workflow_id = _normalise_text(item.get("workflow_id") or item.get("workflow"))
        branch_id = _normalise_text(item.get("branch_id") or workflow_id)
        if not workflow_id or not branch_id:
            continue
        inputs = item.get("inputs")
        branch_inputs = dict(inputs) if isinstance(inputs, Mapping) else {}
        specs.append(
            {
                "branch_id": branch_id,
                "workflow_id": workflow_id,
                "inputs": branch_inputs,
                "max_transitions": _coerce_branch_max_transitions(
                    item.get("max_transitions")
                ),
            }
        )
    return sorted(specs, key=lambda spec: spec["branch_id"])


def _resolve_items_sequence(
    *,
    request: WorkflowActionRequest,
) -> tuple[list[Any] | None, str | None, str | None]:
    explicit_items = request.inputs.get("items")
    if isinstance(explicit_items, Sequence) and not isinstance(
        explicit_items, (str, bytes, bytearray)
    ):
        return list(explicit_items), "inputs.items", None

    items_path = _normalise_text(
        request.inputs.get("items_context_key")
        or request.inputs.get("items_path")
        or request.inputs.get("context_key")
    )
    if not items_path:
        return None, None, "for_each_items_missing"

    found, value = resolve_context_path(context=request.data, path=items_path)
    if not found:
        return None, items_path, f"for_each_items_path_not_found:{items_path}"
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None, items_path, f"for_each_items_not_sequence:{items_path}"
    return list(value), items_path, None


def _emit_signal_result(
    *,
    signal: str,
    scope: str | None,
) -> WorkflowActionResult:
    outputs: Dict[str, Any] = {
        "control_signal": signal,
        "workflow_control": {"signal": signal},
    }
    if scope:
        outputs["control_scope"] = scope
        outputs["workflow_control"]["scope"] = scope
    return WorkflowActionResult(status="success", outputs=outputs)


def _build_break_handler() -> Callable[[WorkflowActionRequest], WorkflowActionResult]:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        loop_scope = _normalise_text(
            request.inputs.get("loop_scope_id") or request.inputs.get("scope")
        )
        return _emit_signal_result(
            signal=WORKFLOW_CONTROL_SIGNAL_BREAK,
            scope=loop_scope or None,
        )

    return _handle


def _extract_declared_output_payload(result: Any) -> dict[str, Any]:
    result_envelope = getattr(result, "result_envelope", None)
    if isinstance(result_envelope, Mapping):
        declared_payload = result_envelope.get("declared_output_payload")
        if isinstance(declared_payload, Mapping):
            return dict(declared_payload)

    data = getattr(result, "data", None)
    if isinstance(data, Mapping):
        last_step = data.get(LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY)
        if isinstance(last_step, Mapping):
            output_payload = last_step.get("output_payload")
            if isinstance(output_payload, Mapping):
                return dict(output_payload)
    return {}


def _child_context_target_payload(child_data: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "concept_id",
        "paper_concept_id",
        "file_copy_concept_id",
        "computer_file_copy_concept_id",
        "source_file_copy_concept_id",
        "represented_artefact_concept_id",
    ):
        value = child_data.get(key)
        if value not in (None, "", [], {}):
            payload[key] = value
    if "concept_id" not in payload and payload.get("represented_artefact_concept_id"):
        payload["concept_id"] = payload["represented_artefact_concept_id"]
    return payload


def _derive_child_step_invocations(
    result: Any,
    *,
    child_definition: WorkflowDefinition,
    child_workflow_id: str,
) -> list[dict[str, Any]]:
    data = getattr(result, "data", None)
    if not isinstance(data, Mapping):
        return []
    context_payload = _child_context_target_payload(data)
    return derive_tool_invocation_records_from_step_envelopes(
        data.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY),
        context_payload=context_payload,
        required_tools=_projectable_child_action_ids(child_definition),
        workflow_id_filter=child_workflow_id,
    )


def _projectable_child_action_ids(definition: WorkflowDefinition) -> list[str]:
    action_ids: list[str] = []
    seen: set[str] = set()
    for state in definition.states.values():
        for action in state.actions:
            action_id = _normalise_text(action.action_id)
            if not action_id:
                continue
            if action_id.lower() == WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID:
                continue
            lowered = action_id.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            action_ids.append(action_id)
    return action_ids


def _build_continue_handler() -> Callable[[WorkflowActionRequest], WorkflowActionResult]:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        loop_scope = _normalise_text(
            request.inputs.get("loop_scope_id") or request.inputs.get("scope")
        )
        return _emit_signal_result(
            signal=WORKFLOW_CONTROL_SIGNAL_CONTINUE,
            scope=loop_scope or None,
        )

    return _handle


def _handle_pause_at_checkpoint(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    """Request one cooperative durable pause after the next saved checkpoint.

    The action authors *where* a durable workflow may pause.  The durable
    executor owns the checkpoint/claim fencing and consumes this request once;
    synchronous workflow execution fails closed because it cannot provide the
    same-instance resume guarantee.
    """

    instance_id = _normalise_text(getattr(request.trace, "instance_id", None))
    trace_metadata = getattr(request.trace, "metadata", None)
    workflow_id = _normalise_text(request.workflow_id)
    state_id = _normalise_text(request.workflow_state_id)
    if (
        not instance_id
        or not isinstance(trace_metadata, Mapping)
        or trace_metadata.get("durable_claim_fenced") is not True
    ):
        return WorkflowActionResult(
            status="failed",
            error="workflow_checkpoint_pause_requires_durable_instance",
        )

    reason_code = _normalise_text(request.inputs.get("reason_code"))
    if not reason_code:
        reason_code = "represented_checkpoint_pause"
    if len(reason_code) > 160:
        return WorkflowActionResult(
            status="failed",
            error="workflow_checkpoint_pause_reason_too_long",
        )

    request_identity = {
        "instance_id": instance_id,
        "workflow_id": workflow_id or None,
        "state_id": state_id or None,
        "reason_code": reason_code,
    }
    request_sha256 = hashlib.sha256(
        json.dumps(
            request_identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    pause_request = {
        "schema_version": WORKFLOW_CHECKPOINT_PAUSE_REQUEST_SCHEMA_VERSION,
        **request_identity,
        "request_sha256": request_sha256,
        "resume_mode": "explicit_same_instance",
    }
    return WorkflowActionResult(
        status="success",
        outputs={WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY: pause_request},
    )


def _build_fork_handler(
    *,
    registry: ActionRegistry,
    definition_loader: Callable[[str], WorkflowDefinition | None],
) -> Callable[[WorkflowActionRequest], WorkflowActionResult]:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        fork_id = _normalise_text(request.inputs.get("fork_id")) or "default"
        failure_policy = _normalise_failure_policy(request.inputs.get("failure_policy"))
        merge_policy = _normalise_merge_policy(request.inputs.get("merge_policy"))
        branch_specs = _normalise_branch_specs(request.inputs.get("branches"))
        max_branches = _coerce_branch_limit()

        if not branch_specs:
            return WorkflowActionResult(status="failed", error="fork_branches_missing")
        if len(branch_specs) > max_branches:
            return WorkflowActionResult(
                status="failed",
                error=f"fork_branch_limit_exceeded:max={max_branches}",
            )

        branch_results: list[dict[str, Any]] = []
        failed_branch: dict[str, Any] | None = None

        for branch in branch_specs:
            child_workflow_id = branch["workflow_id"]
            authority_resolution = resolve_nested_workflow_definition(
                workflow_id=child_workflow_id,
                environment=request.environment,
                fallback_loader=definition_loader,
                execution_scope=request.execution_scope,
            )
            child_definition = authority_resolution.definition
            if child_definition is None:
                authority_error_code = authority_resolution.error_code
                child_error = (
                    f"fork_child_definition_not_found:{child_workflow_id}"
                    if authority_error_code == NESTED_WORKFLOW_DEFINITION_NOT_FOUND
                    else (
                        "fork_child_definition_resolution_failed:"
                        f"{child_workflow_id}:{authority_error_code or 'unknown'}"
                    )
                )
                branch_result = {
                    "branch_id": branch["branch_id"],
                    "workflow_id": child_workflow_id,
                    "completed": False,
                    "final_state": "",
                    "error": child_error,
                    "result": {},
                    "result_envelope": None,
                    "authority_resolution": authority_resolution.to_projection(),
                }
                branch_results.append(branch_result)
                failed_branch = branch_result
                if failure_policy == WORKFLOW_FORK_FAILURE_POLICY_FAIL_FAST:
                    break
                continue

            child_context = dict(request.data)
            child_context.update(branch["inputs"])
            child_context["__workflow_fork_id"] = fork_id
            child_context["__workflow_fork_branch_id"] = branch["branch_id"]

            child_trace = WorkflowExecutionTrace(
                workflow_id=child_workflow_id,
                user_namespace=request.environment.user_namespace,
                metadata={
                    "fork_id": fork_id,
                    "fork_branch_id": branch["branch_id"],
                    "failure_policy": failure_policy,
                    "merge_policy": merge_policy,
                    "authority_resolution": authority_resolution.to_projection(),
                    **build_child_workflow_effect_identity_metadata(
                        request.trace,
                        invocation_kind="fork",
                        parent_workflow_id=request.workflow_id,
                        parent_state_id=request.workflow_state_id,
                        child_workflow_id=child_workflow_id,
                        discriminator=f"{fork_id}:{branch['branch_id']}",
                    ),
                },
            )
            child_result = WorkflowExecutor(
                registry=registry,
                max_transitions=branch["max_transitions"],
            ).run(
                child_definition,
                environment=request.environment,
                data=child_context,
                trace=child_trace,
                _execution_scope=request.execution_scope,
            )

            branch_result = {
                "branch_id": branch["branch_id"],
                "workflow_id": child_workflow_id,
                "completed": bool(child_result.completed),
                "final_state": _normalise_text(child_result.final_state),
                "error": _normalise_text(child_result.error) or None,
                "result": _extract_declared_output_payload(child_result),
                "result_envelope": (
                    dict(child_result.result_envelope)
                    if isinstance(child_result.result_envelope, Mapping)
                    else None
                ),
                "authority_resolution": authority_resolution.to_projection(),
            }
            branch_results.append(branch_result)
            if not child_result.completed:
                failed_branch = branch_result
                if failure_policy == WORKFLOW_FORK_FAILURE_POLICY_FAIL_FAST:
                    break

        success_count = len([item for item in branch_results if item.get("completed")])
        error_count = len(branch_results) - success_count
        fork_snapshot = {
            "fork_id": fork_id,
            "failure_policy": failure_policy,
            "merge_policy": merge_policy,
            "branch_results": branch_results,
            "success_count": success_count,
            "error_count": error_count,
            "joined": False,
        }
        fork_contexts = request.data.get(WORKFLOW_FORK_CONTEXTS_KEY)
        if not isinstance(fork_contexts, dict):
            fork_contexts = {}
            request.data[WORKFLOW_FORK_CONTEXTS_KEY] = fork_contexts
        fork_contexts[fork_id] = fork_snapshot

        increment_runtime_metric(context=request.data, key="fork_invocations")
        append_runtime_event(
            context=request.data,
            event={
                "status": "fork_executed",
                "fork_id": fork_id,
                "failure_policy": failure_policy,
                "merge_policy": merge_policy,
                "success_count": success_count,
                "error_count": error_count,
            },
        )

        outputs: Dict[str, Any] = {
            "fork_id": fork_id,
            "fork_failure_policy": failure_policy,
            "fork_merge_policy": merge_policy,
            "fork_branch_results": branch_results,
            "fork_success_count": success_count,
            "fork_error_count": error_count,
            "fork_partial_success": error_count > 0 and success_count > 0,
        }

        if (
            failure_policy == WORKFLOW_FORK_FAILURE_POLICY_FAIL_FAST
            and failed_branch is not None
        ):
            return WorkflowActionResult(
                status="failed",
                error=(
                    "fork_branch_failed:"
                    f"{failed_branch.get('branch_id')}:{failed_branch.get('error')}"
                ),
                outputs=outputs,
            )

        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _build_for_each_handler(
    *,
    registry: ActionRegistry,
    definition_loader: Callable[[str], WorkflowDefinition | None],
) -> Callable[[WorkflowActionRequest], WorkflowActionResult]:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        child_workflow_id = _normalise_text(
            request.inputs.get("workflow_id") or request.inputs.get("workflow")
        )
        if not child_workflow_id:
            return WorkflowActionResult(
                status="failed",
                error="for_each_workflow_id_missing",
            )
        success_policy = _normalise_for_each_success_policy(
            request.inputs.get("success_policy")
        )
        stop_on_error = _coerce_bool_with_default(
            request.inputs.get("stop_on_error"),
            default=False,
        )
        if (
            stop_on_error
            and success_policy != WORKFLOW_FOR_EACH_SUCCESS_POLICY_ALL_MUST_SUCCEED
        ):
            return WorkflowActionResult(
                status="failed",
                error="for_each_stop_on_error_requires_all_must_succeed",
                outputs={
                    "for_each_success_policy": success_policy,
                    "for_each_stop_on_error": True,
                    "for_each_item_count": 0,
                    "iteration_results": [],
                    "iteration_errors": [],
                    "successful_results": [],
                    "invocations": [],
                },
            )

        authority_resolution = resolve_nested_workflow_definition(
            workflow_id=child_workflow_id,
            environment=request.environment,
            fallback_loader=definition_loader,
            execution_scope=request.execution_scope,
        )
        child_definition = authority_resolution.definition
        if child_definition is None:
            authority_error_code = authority_resolution.error_code
            error = (
                f"for_each_definition_not_found:{child_workflow_id}"
                if authority_error_code == NESTED_WORKFLOW_DEFINITION_NOT_FOUND
                else (
                    "for_each_definition_resolution_failed:"
                    f"{child_workflow_id}:{authority_error_code or 'unknown'}"
                )
            )
            return WorkflowActionResult(
                status="failed",
                error=error,
                outputs={
                    "for_each_authority_resolution": (
                        authority_resolution.to_projection()
                    )
                },
            )

        items, items_source, items_error = _resolve_items_sequence(request=request)
        if items_error:
            return WorkflowActionResult(status="failed", error=items_error)
        items = items or []

        item_limit = _coerce_for_each_limit(request.inputs.get("max_items"))
        selected_items = list(items[:item_limit])
        item_context_key = (
            _normalise_text(request.inputs.get("item_context_key")) or "current_item"
        )
        index_context_key = (
            _normalise_text(request.inputs.get("index_context_key")) or "index"
        )
        include_tool_invocations_in_iteration_results = _coerce_bool_with_default(
            request.inputs.get("include_tool_invocations_in_iteration_results"),
            default=True,
        )
        max_transitions = _coerce_branch_max_transitions(
            request.inputs.get("max_transitions")
        )
        max_concurrency = min(
            _coerce_for_each_max_concurrency(request.inputs.get("max_concurrency")),
            len(selected_items) or 1,
        )
        if stop_on_error and max_concurrency > 1:
            return WorkflowActionResult(
                status="failed",
                error="for_each_stop_on_error_requires_sequential_execution",
                outputs={
                    "items_source": items_source or None,
                    "for_each_item_count": 0,
                    "for_each_selected_item_count": len(selected_items),
                    "for_each_unattempted_count": len(selected_items),
                    "for_each_item_limit": item_limit,
                    "for_each_max_concurrency": max_concurrency,
                    "for_each_success_policy": success_policy,
                    "for_each_stop_on_error": True,
                    "for_each_stopped_on_error": False,
                    "for_each_stopped_early": False,
                    "for_each_include_tool_invocations_in_iteration_results": (
                        include_tool_invocations_in_iteration_results
                    ),
                    "for_each_success_count": 0,
                    "for_each_error_count": 0,
                    "for_each_partial_success": False,
                    "for_each_authority_resolution": (
                        authority_resolution.to_projection()
                    ),
                    "iteration_results": [],
                    "iteration_errors": [],
                    "successful_results": [],
                    "invocations": [],
                },
            )

        def _execute_item(index: int, item: Any) -> dict[str, Any]:
            child_context = dict(request.data)
            child_context.pop(WORKFLOW_STEP_RESULT_ENVELOPES_KEY, None)
            child_context.pop("invocations", None)
            child_context[item_context_key] = item
            child_context[index_context_key] = index
            child_context["__workflow_for_each_parent_workflow_id"] = _normalise_text(
                request.inputs.get("__parent_workflow_id")
            )
            child_context["__workflow_for_each_parent_state_id"] = _normalise_text(
                request.inputs.get("__parent_state_id")
            )
            child_context["__workflow_for_each_items_source"] = items_source or None

            child_trace = WorkflowExecutionTrace(
                workflow_id=child_workflow_id,
                user_namespace=request.environment.user_namespace,
                metadata={
                    "for_each_parent_workflow_id": _normalise_text(
                        request.inputs.get("__parent_workflow_id")
                    )
                    or None,
                    "for_each_parent_state_id": _normalise_text(
                        request.inputs.get("__parent_state_id")
                    )
                    or None,
                    "items_source": items_source or None,
                    "item_context_key": item_context_key,
                    "index_context_key": index_context_key,
                    "item_index": index,
                    "success_policy": success_policy,
                    "stop_on_error": stop_on_error,
                    "authority_resolution": authority_resolution.to_projection(),
                    **build_child_workflow_effect_identity_metadata(
                        request.trace,
                        invocation_kind="for_each",
                        parent_workflow_id=request.workflow_id,
                        parent_state_id=request.workflow_state_id,
                        child_workflow_id=child_workflow_id,
                        discriminator=index,
                    ),
                },
            )
            child_result = WorkflowExecutor(
                registry=registry,
                max_transitions=max_transitions,
            ).run(
                child_definition,
                environment=request.environment,
                data=child_context,
                trace=child_trace,
                _execution_scope=request.execution_scope,
            )
            child_invocations = _derive_child_step_invocations(
                child_result,
                child_definition=child_definition,
                child_workflow_id=child_workflow_id,
            )
            return {
                "index": index,
                "item": item,
                "completed": bool(child_result.completed),
                "final_state": _normalise_text(child_result.final_state),
                "error": _normalise_text(child_result.error) or None,
                "tool_invocations": child_invocations,
                "result": _extract_declared_output_payload(child_result),
                "result_envelope": (
                    dict(child_result.result_envelope)
                    if isinstance(child_result.result_envelope, Mapping)
                    else None
                ),
            }

        indexed_results: dict[int, dict[str, Any]] = {}
        if max_concurrency <= 1 or len(selected_items) <= 1:
            for index, item in enumerate(selected_items):
                indexed_results[index] = _execute_item(index, item)
                if stop_on_error and not indexed_results[index]["completed"]:
                    break
        else:
            with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
                future_to_index = {
                    executor.submit(_execute_item, index, item): index
                    for index, item in enumerate(selected_items)
                }
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    try:
                        indexed_results[index] = future.result()
                    except Exception as exc:
                        indexed_results[index] = {
                            "index": index,
                            "item": selected_items[index],
                            "completed": False,
                            "final_state": None,
                            "error": f"for_each_item_exception:{exc}",
                            "tool_invocations": [],
                            "result": {},
                            "result_envelope": None,
                        }

        raw_iteration_results = [
            indexed_results[index]
            for index in range(len(selected_items))
            if index in indexed_results
        ]
        child_step_invocations: list[Mapping[str, Any]] = []
        for item in raw_iteration_results:
            raw_item_invocations = item.get("tool_invocations")
            if not isinstance(raw_item_invocations, list):
                continue
            child_step_invocations.extend(
                invocation
                for invocation in raw_item_invocations
                if isinstance(invocation, Mapping)
            )
        iteration_results: list[dict[str, Any]] = []
        for item in raw_iteration_results:
            public_item = dict(item)
            if not include_tool_invocations_in_iteration_results:
                public_item.pop("tool_invocations", None)
            iteration_results.append(public_item)

        success_count = len([item for item in iteration_results if item["completed"]])
        error_count = len(iteration_results) - success_count
        unattempted_count = len(selected_items) - len(iteration_results)
        stopped_on_error = stop_on_error and error_count > 0
        stopped_early = stopped_on_error and unattempted_count > 0
        successful_results = [
            dict(result_payload)
            for item in iteration_results
            if item["completed"]
            and isinstance((result_payload := item.get("result")), Mapping)
            and result_payload
        ]
        iteration_errors: list[dict[str, Any]] = []
        for item in iteration_results:
            if item["completed"]:
                continue
            failure: dict[str, Any] = {
                "index": item.get("index"),
                "item": item.get("item"),
                "final_state": item.get("final_state"),
                "error": item.get("error"),
            }
            declared_failure_result = item.get("result")
            if isinstance(declared_failure_result, Mapping) and declared_failure_result:
                failure["result"] = dict(declared_failure_result)
            iteration_errors.append(failure)
        final_state_counts: dict[str, int] = {}
        for item in iteration_results:
            final_state = _normalise_text(item.get("final_state")) or "unknown"
            final_state_counts[final_state] = final_state_counts.get(final_state, 0) + 1
        outputs: Dict[str, Any] = {
            "items_source": items_source or None,
            "for_each_item_count": len(iteration_results),
            "for_each_selected_item_count": len(selected_items),
            "for_each_unattempted_count": unattempted_count,
            "for_each_item_limit": item_limit,
            "for_each_max_concurrency": max_concurrency,
            "for_each_success_policy": success_policy,
            "for_each_stop_on_error": stop_on_error,
            "for_each_stopped_on_error": stopped_on_error,
            "for_each_stopped_early": stopped_early,
            "for_each_include_tool_invocations_in_iteration_results": (
                include_tool_invocations_in_iteration_results
            ),
            "for_each_success_count": success_count,
            "for_each_error_count": error_count,
            "for_each_partial_success": success_count > 0 and error_count > 0,
            "for_each_final_state_counts": final_state_counts,
            "for_each_authority_resolution": authority_resolution.to_projection(),
            "iteration_results": iteration_results,
            "iteration_errors": iteration_errors,
            "successful_results": successful_results,
            "invocations": [],
        }
        existing_invocations = request.data.get("invocations")
        if isinstance(existing_invocations, list) or child_step_invocations:
            outputs["invocations"] = [
                *(
                    item
                    for item in (
                        existing_invocations
                        if isinstance(existing_invocations, list)
                        else []
                    )
                    if isinstance(item, Mapping)
                ),
                *child_step_invocations,
            ]

        increment_runtime_metric(context=request.data, key="for_each_invocations")
        append_runtime_event(
            context=request.data,
            event={
                "status": "for_each_executed",
                "workflow_id": child_workflow_id,
                "item_count": len(iteration_results),
                "item_limit": item_limit,
                "max_concurrency": max_concurrency,
                "success_count": success_count,
                "error_count": error_count,
                "unattempted_count": unattempted_count,
                "success_policy": success_policy,
                "stop_on_error": stop_on_error,
                "stopped_on_error": stopped_on_error,
                "stopped_early": stopped_early,
                "include_tool_invocations_in_iteration_results": (
                    include_tool_invocations_in_iteration_results
                ),
                "items_source": items_source or None,
            },
        )

        if (
            success_policy == WORKFLOW_FOR_EACH_SUCCESS_POLICY_ALL_MUST_SUCCEED
            and error_count > 0
        ):
            return WorkflowActionResult(
                status="failed",
                error=f"for_each_item_failed:{child_workflow_id}",
                outputs=outputs,
            )

        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _build_join_handler() -> Callable[[WorkflowActionRequest], WorkflowActionResult]:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        fork_id = _normalise_text(request.inputs.get("fork_id"))
        if not fork_id:
            return WorkflowActionResult(status="failed", error="join_fork_id_missing")

        fork_contexts = request.data.get(WORKFLOW_FORK_CONTEXTS_KEY)
        if not isinstance(fork_contexts, Mapping):
            return WorkflowActionResult(
                status="failed",
                error=f"join_without_matching_fork:{fork_id}",
            )
        fork_snapshot_raw = fork_contexts.get(fork_id)
        fork_snapshot = (
            dict(fork_snapshot_raw)
            if isinstance(fork_snapshot_raw, Mapping)
            else None
        )
        if fork_snapshot is None:
            return WorkflowActionResult(
                status="failed",
                error=f"join_without_matching_fork:{fork_id}",
            )

        branch_results_raw = fork_snapshot.get("branch_results")
        branch_results = (
            list(branch_results_raw)
            if isinstance(branch_results_raw, Sequence)
            and not isinstance(branch_results_raw, (str, bytes, bytearray))
            else []
        )
        ordered_successes = sorted(
            [
                item
                for item in branch_results
                if isinstance(item, Mapping) and bool(item.get("completed"))
            ],
            key=lambda item: _normalise_text(item.get("branch_id")),
        )
        errors = [
            item
            for item in branch_results
            if isinstance(item, Mapping) and not bool(item.get("completed"))
        ]

        merged: Dict[str, Any] = {}
        for entry in ordered_successes:
            payload = entry.get("result")
            if isinstance(payload, Mapping):
                merged.update(payload)

        failure_policy = _normalise_failure_policy(fork_snapshot.get("failure_policy"))
        fork_snapshot["joined"] = True
        fork_snapshot["joined_result"] = dict(merged)
        if isinstance(fork_contexts, dict):
            fork_contexts[fork_id] = fork_snapshot

        increment_runtime_metric(context=request.data, key="join_invocations")
        append_runtime_event(
            context=request.data,
            event={
                "status": "join_executed",
                "fork_id": fork_id,
                "success_count": len(ordered_successes),
                "error_count": len(errors),
                "failure_policy": failure_policy,
            },
        )

        outputs: Dict[str, Any] = {
            "fork_id": fork_id,
            "result": merged,
            "fork_joined": True,
            "fork_error_count": len(errors),
            "fork_success_count": len(ordered_successes),
            "fork_branch_results": branch_results,
        }
        if errors and failure_policy in {
            WORKFLOW_FORK_FAILURE_POLICY_FAIL_FAST,
            WORKFLOW_FORK_FAILURE_POLICY_COLLECT_ERRORS,
        }:
            return WorkflowActionResult(
                status="failed",
                error=f"join_branch_errors:{fork_id}",
                outputs=outputs,
            )

        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _handle_context_set(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Set key-value pairs in workflow context.

    Input schema::

        {
          "assignments": [
            {"key": "my_flag", "value": true},
            {"key": "other_key", "value_from_context": "source_key"},
            {
              "key": "title",
              "value_from_context_options": ["title", "metadata.title"],
              "skip_if_unresolved": true
            }
          ]
        }

    Each assignment writes exactly one context key.  ``value`` sets a literal;
    ``value_from_context`` copies from an existing context key, and
    ``value_from_context_options`` copies the first present, non-empty value
    from a list of candidate context paths.
    """
    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}
    assignments = inputs.get("assignments")
    if not isinstance(assignments, (list, tuple)):
        return WorkflowActionResult(
            status="failed",
            error="context_set:assignments_missing_or_invalid",
        )

    outputs: dict[str, Any] = {}
    applied: list[str] = []
    for index, entry in enumerate(assignments):
        if not isinstance(entry, Mapping):
            return WorkflowActionResult(
                status="failed",
                error=f"context_set:assignment_{index}_not_mapping",
            )
        key = str(entry.get("key") or "").strip()
        if not key:
            return WorkflowActionResult(
                status="failed",
                error=f"context_set:assignment_{index}_missing_key",
            )

        if _coerce_bool(entry.get("preserve_existing")):
            found_existing, existing = resolve_context_path(
                context=request.data,
                path=key,
            )
            if found_existing and _context_value_present(existing):
                continue

        value_found = True
        if "value_from_context_options" in entry:
            options = entry.get("value_from_context_options")
            if not isinstance(options, (list, tuple)):
                return WorkflowActionResult(
                    status="failed",
                    error=(
                        f"context_set:assignment_{index}_"
                        "value_from_context_options_not_sequence"
                    ),
                )
            value_found = False
            value = None
            for raw_source_key in options:
                source_key = str(raw_source_key or "").strip()
                if not source_key:
                    continue
                found, resolved = resolve_context_path(
                    context=request.data,
                    path=source_key,
                )
                if found and _context_value_present(resolved):
                    value = resolved
                    value_found = True
                    break
        elif "value_from_context" in entry:
            source_key = str(entry["value_from_context"]).strip()
            found, resolved = resolve_context_path(
                context=request.data, path=source_key
            )
            value = resolved if found else None
            value_found = found
        else:
            value = entry.get("value")

        if not value_found and _coerce_bool(entry.get("skip_if_unresolved")):
            continue

        outputs[key] = value
        applied.append(key)

    outputs["_context_set_applied_keys"] = applied
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_kr_materialisation_guard(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    """Apply an optional represented KR write-boundary contract."""

    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}

    def _input_or_context(name: str, default_path: str) -> Any:
        if name in inputs:
            return inputs.get(name)
        path = _normalise_text(inputs.get(f"{name}_context_key")) or default_path
        found, value = resolve_context_path(context=request.data, path=path)
        return value if found else None

    outputs = validate_kr_materialisation_guard(
        guard_contract=_input_or_context(
            "guard_contract", "kr_materialisation_guard"
        ),
        phase=_normalise_text(inputs.get("phase")) or "plan",
        concept_specs=_input_or_context("concept_specs", "kr_concept_specs"),
        relationship_specs=_input_or_context(
            "relationship_specs", "kr_relationship_specs"
        ),
        concept_iteration_results=_input_or_context(
            "concept_iteration_results", "kr_concept_iteration_results"
        ),
        resolved_relationship_specs=_input_or_context(
            "resolved_relationship_specs", "kr_resolved_relationship_specs"
        ),
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _kr_resolution_outcome(
    *,
    decision: str,
    resolved_relationship_specs: Sequence[Mapping[str, Any]] = (),
    blocking_reason: str | None = None,
) -> WorkflowActionResult:
    return WorkflowActionResult(
        status="success",
        outputs={
            "decision": decision,
            "resolved_relationship_specs": [
                dict(item) for item in resolved_relationship_specs
            ],
            "blocking_reason": blocking_reason,
        },
    )


def _kr_sequence(value: Any) -> list[Any] | None:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return list(value)
    return None


def _kr_concept_spec_key(spec: Mapping[str, Any]) -> str:
    return _normalise_text(
        spec.get("key")
        or spec.get("target_key")
        or spec.get("target_name")
        or spec.get("name")
    )


def _kr_verified_concept_ids(
    concept_iteration_results: Any,
) -> tuple[dict[str, str] | None, str | None]:
    rows = _kr_sequence(concept_iteration_results)
    if rows is None:
        return None, "kr_relationship_resolution_concept_results_invalid"
    if len(rows) > _MAX_KR_RELATIONSHIP_RESOLUTION_CONCEPT_RESULTS:
        return None, "kr_relationship_resolution_concept_results_bound_exceeded"

    verified: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("completed") is not True:
            return None, "kr_relationship_resolution_concept_result_unverified"
        item = row.get("item")
        result = row.get("result")
        if not isinstance(item, Mapping) or not isinstance(result, Mapping):
            return None, "kr_relationship_resolution_concept_result_unverified"
        item_key = _kr_concept_spec_key(item)
        result_key = _normalise_text(result.get("kr_concept_key"))
        key = result_key or item_key
        concept_id = _normalise_text(result.get("kr_concept_id"))
        readback_id = _normalise_text(result.get("kr_readback_concept_id"))
        if (
            not key
            or key in verified
            or (item_key and result_key and item_key != result_key)
            or not concept_id.startswith("#V#")
            or readback_id != concept_id
        ):
            return None, "kr_relationship_resolution_concept_result_unverified"
        verified[key] = concept_id
    return verified, None


def _kr_resolve_endpoint(
    spec: Mapping[str, Any],
    *,
    side: str,
    verified_by_key: Mapping[str, str],
) -> tuple[str | None, str | None]:
    key = _normalise_text(spec.get(f"{side}_key"))
    concept_id = _normalise_text(spec.get(f"{side}_id"))
    if bool(key) == bool(concept_id):
        return None, f"kr_relationship_resolution_{side}_reference_invalid"
    if key:
        resolved = _normalise_text(verified_by_key.get(key))
        if not resolved:
            return None, f"kr_relationship_resolution_{side}_key_unresolved"
        return resolved, None
    if not concept_id.startswith("#V#"):
        return None, f"kr_relationship_resolution_{side}_id_invalid"
    return concept_id, None


def _kr_relationship_predicate(
    spec: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    predicate = _normalise_text(spec.get("predicate"))
    predicate_id = _normalise_text(spec.get("predicate_id"))
    if predicate and predicate_id and predicate != predicate_id:
        return None, "kr_relationship_resolution_predicate_conflict"
    resolved = predicate or predicate_id
    if resolved in {
        "type_of",
        "instance_of",
        "is_a_type_of",
        "is_an_instance_of",
    } or resolved.startswith("#V#"):
        return resolved, None
    return None, "kr_relationship_resolution_predicate_invalid"


def _handle_kr_relationship_resolution(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    """Resolve exact relationship specs against verified concept child results."""

    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}

    def _input_or_context(name: str, default_path: str) -> Any:
        if name in inputs:
            return inputs.get(name)
        path = _normalise_text(inputs.get(f"{name}_context_key")) or default_path
        found, value = resolve_context_path(context=request.data, path=path)
        return value if found else None

    raw_specs = _kr_sequence(
        _input_or_context("relationship_specs", "kr_relationship_specs")
    )
    if raw_specs is None:
        return _kr_resolution_outcome(
            decision="block",
            blocking_reason="kr_relationship_resolution_specs_invalid",
        )
    if len(raw_specs) > _MAX_KR_RELATIONSHIP_RESOLUTION_SPECS:
        return _kr_resolution_outcome(
            decision="block",
            blocking_reason="kr_relationship_resolution_specs_bound_exceeded",
        )
    if not raw_specs:
        return _kr_resolution_outcome(decision="skip")

    verified_by_key, concept_error = _kr_verified_concept_ids(
        _input_or_context(
            "concept_iteration_results", "kr_concept_iteration_results"
        )
    )
    if verified_by_key is None:
        return _kr_resolution_outcome(
            decision="block",
            blocking_reason=(
                concept_error
                or "kr_relationship_resolution_concept_results_invalid"
            ),
        )

    resolved_specs: list[dict[str, Any]] = []
    seen_triples: set[tuple[str, str, str]] = set()
    seen_keys: set[str] = set()
    for index, raw_spec in enumerate(raw_specs):
        if not isinstance(raw_spec, Mapping):
            return _kr_resolution_outcome(
                decision="block",
                blocking_reason="kr_relationship_resolution_spec_invalid",
            )
        source_id, source_error = _kr_resolve_endpoint(
            raw_spec,
            side="source",
            verified_by_key=verified_by_key,
        )
        target_id, target_error = _kr_resolve_endpoint(
            raw_spec,
            side="target",
            verified_by_key=verified_by_key,
        )
        predicate, predicate_error = _kr_relationship_predicate(raw_spec)
        if source_error or target_error or predicate_error:
            return _kr_resolution_outcome(
                decision="block",
                blocking_reason=source_error or target_error or predicate_error,
            )
        if source_id is None or target_id is None or predicate is None:
            return _kr_resolution_outcome(
                decision="block",
                blocking_reason="kr_relationship_resolution_spec_unresolved",
            )
        relationship_key = _normalise_text(
            raw_spec.get("key") or raw_spec.get("relationship_key")
        ) or f"relationship_{index + 1}"
        triple = (source_id, predicate, target_id)
        if relationship_key in seen_keys:
            return _kr_resolution_outcome(
                decision="block",
                blocking_reason="kr_relationship_resolution_duplicate_key",
            )
        if triple in seen_triples:
            return _kr_resolution_outcome(
                decision="block",
                blocking_reason="kr_relationship_resolution_duplicate_relationship",
            )
        seen_keys.add(relationship_key)
        seen_triples.add(triple)
        resolved_specs.append(
            {
                "key": relationship_key,
                "source_id": source_id,
                "predicate": predicate,
                "target_id": target_id,
                "rationale": _normalise_text(raw_spec.get("rationale")),
            }
        )

    return _kr_resolution_outcome(
        decision="assert",
        resolved_relationship_specs=resolved_specs,
    )


def _handle_kr_relationship_readback(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    """Verify the exact relationship tuple after canonical endpoint read-back."""

    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}

    def _input_or_context(name: str, default_path: str) -> Any:
        if name in inputs:
            return inputs.get(name)
        path = _normalise_text(inputs.get(f"{name}_context_key")) or default_path
        found, value = resolve_context_path(context=request.data, path=path)
        return value if found else None

    outputs = verify_kr_relationship_readback(
        expected_source_id=_input_or_context(
            "expected_source_id", "kr_relationship_source_id"
        ),
        expected_predicate_id=_input_or_context(
            "expected_predicate_id", "kr_relationship_predicate"
        ),
        expected_target_id=_input_or_context(
            "expected_target_id", "kr_relationship_target_id"
        ),
        assertion_succeeded=_input_or_context(
            "assertion_succeeded", "kr_relationship_assert_success"
        ),
        source_readback_id=_input_or_context(
            "source_readback_id", "kr_relationship_source_readback_id"
        ),
        source_readback_relationships=_input_or_context(
            "source_readback_relationships",
            "kr_relationship_source_readback_relationships",
        ),
        target_readback_id=_input_or_context(
            "target_readback_id", "kr_relationship_target_readback_id"
        ),
    )
    outputs.setdefault("verified_relationship", None)
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_relationship_effect_readback(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    """Match successful relationship writes to exact canonical read-back.

    The workflow supplies the domain-specific source and predicate. This
    reusable mechanism binds successful mutation targets from the current LLM
    tool history to later canonical relation hits. Single-target matching is
    the default; represented workflows may opt into bounded multi-target mode.
    """

    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}
    outputs = verify_relationship_effect_readback(
        mutation_tool_name=inputs.get("mutation_tool_name"),
        expected_source_id=inputs.get("expected_source_id"),
        expected_predicate_id=inputs.get("expected_predicate_id"),
        expected_relation_kind=inputs.get("expected_relation_kind"),
        tool_invocations=inputs.get("tool_invocations"),
        relationship_effect_receipt=inputs.get("relationship_effect_receipt"),
        readback_concept_id=inputs.get("readback_concept_id"),
        readback_total_hits=inputs.get("readback_total_hits"),
        readback_hits=inputs.get("readback_hits"),
        readback_total_hits_is_lower_bound=inputs.get(
            "readback_total_hits_is_lower_bound",
            False,
        ),
        allow_multiple_targets=inputs.get("allow_multiple_targets", False),
        minimum_unique_targets=inputs.get("minimum_unique_targets", 1),
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_text_effect_readback(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    """Correlate current-run text writes with canonical text read-back."""

    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}
    outputs = verify_text_effect_readback(
        required_predicates=inputs.get("required_predicates"),
        optional_predicates=inputs.get("optional_predicates"),
        tool_invocations=inputs.get("tool_invocations"),
        text_effect_receipts=inputs.get("text_effect_receipts"),
        expected_concept_id=inputs.get("expected_concept_id"),
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _normalise_field_name(value: Any) -> str:
    return str(value or "").strip()


def _normalise_field_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates: Sequence[Any] = [item.strip() for item in value.split(",")]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        candidates = value
    else:
        candidates = [value]
    fields: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        field = _normalise_field_name(candidate)
        if not field:
            continue
        key = field.lower()
        if key in seen:
            continue
        seen.add(key)
        fields.append(field)
    return fields


def _normalise_field_aliases(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    aliases: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _normalise_field_name(raw_key).lower()
        target = _normalise_field_name(raw_value)
        if key and target:
            aliases[key] = target
    return aliases


def _truncate_projected_value(value: Any, max_chars: int | None) -> tuple[Any, bool]:
    if not isinstance(value, str) or not max_chars or max_chars <= 0:
        return value, False
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


def _handle_context_project(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Project candidate context fields to a compact payload.

    The projection policy is supplied by workflow context/metadata. This action
    is intentionally domain-neutral: it only applies the represented field list,
    aliases, exclusions, and size bounds supplied by the workflow.
    """

    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}
    target_key = _normalise_text(inputs.get("target_key")) or "projected_payload"
    field_sources = inputs.get("field_sources")
    if not isinstance(field_sources, Mapping):
        return WorkflowActionResult(
            status="failed",
            error="context_project:field_sources_missing_or_invalid",
        )

    requirement = inputs.get("requirement")
    requirement_map = requirement if isinstance(requirement, Mapping) else {}
    required_fields = _normalise_field_sequence(
        inputs.get("required_fields")
        or requirement_map.get("required_fields")
        or requirement_map.get("fields")
    )
    if not required_fields:
        required_fields = _normalise_field_sequence(
            inputs.get("default_fields") or requirement_map.get("default_fields")
        )
    always_include_fields = _normalise_field_sequence(
        inputs.get("always_include_fields")
        or requirement_map.get("always_include_fields")
    )
    selected_fields = _normalise_field_sequence(
        [*always_include_fields, *required_fields]
    )
    excluded_fields = {
        field.lower()
        for field in _normalise_field_sequence(
            inputs.get("excluded_fields") or requirement_map.get("excluded_fields")
        )
    }
    aliases = _normalise_field_aliases(
        inputs.get("field_aliases") or requirement_map.get("field_aliases")
    )
    include_null_fields = _coerce_bool(
        inputs.get(
            "include_null_fields",
            requirement_map.get("include_null_fields"),
        )
    )
    include_empty_fields = _coerce_bool(
        inputs.get(
            "include_empty_fields",
            requirement_map.get("include_empty_fields"),
        )
    )

    try:
        max_chars_per_field = int(
            inputs.get("max_chars_per_field")
            or requirement_map.get("max_chars_per_field")
            or 0
        )
    except (TypeError, ValueError):
        max_chars_per_field = 0

    projected: dict[str, Any] = {}
    selected_output_fields: list[str] = []
    missing_fields: list[str] = []
    omitted_fields: list[str] = []
    truncated_fields: list[str] = []
    source_lookup = {
        _normalise_field_name(key).lower(): (key, value)
        for key, value in field_sources.items()
        if _normalise_field_name(key)
    }

    for requested_field in selected_fields:
        requested_key = requested_field.lower()
        output_field = aliases.get(requested_key, requested_field)
        output_key = output_field.lower()
        if requested_key in excluded_fields or output_key in excluded_fields:
            omitted_fields.append(requested_field)
            continue
        source_entry = source_lookup.get(output_key) or source_lookup.get(requested_key)
        if source_entry is None:
            missing_fields.append(requested_field)
            continue
        _source_key, value = source_entry
        if value is None and include_null_fields:
            projected[output_field] = None
            selected_output_fields.append(output_field)
            continue
        if (
            include_empty_fields
            and isinstance(value, (list, tuple, set, frozenset, dict))
            and not value
        ):
            projected[output_field] = value
            selected_output_fields.append(output_field)
            continue
        if not _context_value_present(value):
            missing_fields.append(requested_field)
            continue
        projected_value, truncated = _truncate_projected_value(
            value,
            max_chars_per_field if max_chars_per_field > 0 else None,
        )
        projected[output_field] = projected_value
        selected_output_fields.append(output_field)
        if truncated:
            truncated_fields.append(output_field)

    if missing_fields and _coerce_bool(inputs.get("include_missing_fields")):
        projected["_missing_fields"] = list(missing_fields)

    metadata = {
        "schema_version": "workflow_item_output_projection.v1",
        "target_key": target_key,
        "selected_fields": selected_output_fields,
        "requested_fields": selected_fields,
        "missing_fields": missing_fields,
        "omitted_fields": omitted_fields,
        "truncated_fields": truncated_fields,
        "include_null_fields": include_null_fields,
        "include_empty_fields": include_empty_fields,
        "requirement_schema_version": _normalise_text(
            requirement_map.get("schema_version")
        )
        or None,
        "purpose": _normalise_text(requirement_map.get("purpose")) or None,
    }
    outputs: dict[str, Any] = {
        target_key: projected,
        f"{target_key}_projection": metadata,
    }
    append_runtime_event(
        context=request.data,
        event={
            "status": "context_projected",
            "target_key": target_key,
            "selected_fields": list(selected_output_fields),
            "missing_fields": list(missing_fields),
            "purpose": metadata["purpose"],
        },
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _request_lookup_context(request: WorkflowActionRequest) -> dict[str, Any]:
    env = request.environment
    return {
        "action_id": request.action_id,
        "workflow_id": request.workflow_id,
        "workflow_state_id": request.workflow_state_id,
        "action_target_id": request.action_target_id,
        "contract_concept_id": request.contract_concept_id,
        "execution_mode": request.execution_mode,
        "environment": {
            "model": env.model,
            "user_namespace": env.user_namespace,
            "user_concept_id": env.user_concept_id,
            "org_concept_id": env.org_concept_id,
            "default_gmail_profile": env.default_gmail_profile,
        },
    }


def _resolve_context_template_source(
    *,
    request: WorkflowActionRequest,
    entry: Mapping[str, Any],
) -> tuple[bool, Any]:
    if "value" in entry:
        return True, entry.get("value")

    if "value_from_context_options" in entry:
        options = entry.get("value_from_context_options")
        if not isinstance(options, (list, tuple)):
            return False, None
        for raw_path in options:
            path = str(raw_path or "").strip()
            if not path:
                continue
            found, value = resolve_context_path(context=request.data, path=path)
            if found and _context_value_present(value):
                return True, value

    if "value_from_context" in entry:
        path = str(entry.get("value_from_context") or "").strip()
        if path:
            found, value = resolve_context_path(context=request.data, path=path)
            if found and _context_value_present(value):
                return True, value

    if "value_from_request" in entry:
        path = str(entry.get("value_from_request") or "").strip()
        if path:
            found, value = resolve_context_path(
                context=_request_lookup_context(request),
                path=path,
            )
            if found and _context_value_present(value):
                return True, value

    if "value_from_environment" in entry:
        attr = str(entry.get("value_from_environment") or "").strip()
        if attr:
            value = getattr(request.environment, attr, None)
            if _context_value_present(value):
                return True, value

    if "default" in entry:
        return True, entry.get("default")

    return False, None


def _format_context_template_value(value: Any, transform: str) -> str:
    mode = str(transform or "string").strip().lower()
    if mode == "json":
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    if mode == "concept_id":
        from ...utils.concept_id_utils import canonicalise_vontology_concept_id

        return canonicalise_vontology_concept_id(str(value or "")) or ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return "" if value is None else str(value)


def _handle_context_template(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Render context-derived strings into workflow context.

    Input schema::

        {
          "assignments": [
            {
              "key": "rendered_key",
              "template": "Profile: {workflow_id} / {model_ref}",
              "variables": {
                "workflow_id": {"value_from_request": "workflow_id"},
                "model_ref": {
                  "value_from_environment": "model",
                  "default": "unknown_model"
                }
              }
            }
          ]
        }

    Variables support literal ``value``, context paths, request paths,
    environment attributes, defaults, and compact ``json`` / ``concept_id``
    rendering. The action is deliberately generic and contains no workflow-
    specific policy.
    """

    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}
    assignments = inputs.get("assignments")
    if not isinstance(assignments, (list, tuple)):
        return WorkflowActionResult(
            status="failed",
            error="context_template:assignments_missing_or_invalid",
        )

    outputs: dict[str, Any] = {}
    applied: list[str] = []
    for index, entry in enumerate(assignments):
        if not isinstance(entry, Mapping):
            return WorkflowActionResult(
                status="failed",
                error=f"context_template:assignment_{index}_not_mapping",
            )

        key = str(entry.get("key") or "").strip()
        if not key:
            return WorkflowActionResult(
                status="failed",
                error=f"context_template:assignment_{index}_missing_key",
            )

        if _coerce_bool(entry.get("preserve_existing")):
            found_existing, existing = resolve_context_path(
                context=request.data,
                path=key,
            )
            if found_existing and _context_value_present(existing):
                continue

        template = entry.get("template")
        if not isinstance(template, str) or not template:
            return WorkflowActionResult(
                status="failed",
                error=f"context_template:assignment_{index}_missing_template",
            )

        raw_variables = entry.get("variables")
        variables = raw_variables if isinstance(raw_variables, Mapping) else {}
        rendered_variables: dict[str, str] = {}
        unresolved: list[str] = []
        for raw_name, raw_spec in variables.items():
            name = str(raw_name or "").strip()
            if not name:
                continue
            spec = raw_spec if isinstance(raw_spec, Mapping) else {"value": raw_spec}
            found, value = _resolve_context_template_source(
                request=request,
                entry=spec,
            )
            if not found:
                unresolved.append(name)
                continue
            transform = str(spec.get("transform") or spec.get("format") or "string")
            rendered_variables[name] = _format_context_template_value(
                value,
                transform,
            )

        if unresolved:
            if _coerce_bool(entry.get("skip_if_unresolved")):
                continue
            return WorkflowActionResult(
                status="failed",
                error=(
                    f"context_template:assignment_{index}_"
                    f"unresolved_variables:{','.join(unresolved)}"
                ),
            )

        try:
            rendered = template.format_map(rendered_variables)
        except KeyError as exc:
            if _coerce_bool(entry.get("skip_if_unresolved")):
                continue
            return WorkflowActionResult(
                status="failed",
                error=(
                    f"context_template:assignment_{index}_"
                    f"unresolved_template_variable:{exc.args[0]}"
                ),
            )

        transform = str(entry.get("transform") or "string")
        outputs[key] = _format_context_template_value(rendered, transform)
        applied.append(key)

    outputs["_context_template_applied_keys"] = applied
    return WorkflowActionResult(status="success", outputs=outputs)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _context_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return bool(value)
    return True


def register_control_flow_actions(
    registry: ActionRegistry,
    *,
    definition_loader: Callable[[str], WorkflowDefinition | None] | None = None,
) -> None:
    """Register reusable control-flow primitives in the action registry."""

    loader = definition_loader or load_workflow_definition_from_vontology
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
            handler=_handle_context_set,
            description=(
                "Declaratively set key-value pairs in workflow context.  "
                "Accepts an ``assignments`` list of dicts, each with ``key`` "
                "and one of ``value`` (literal) or ``value_from_context`` "
                "(copy from another context key)."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_CONTEXT_TEMPLATE_ID,
            handler=_handle_context_template,
            description=(
                "Declaratively render workflow context/request/environment "
                "values into string context keys. Supports JSON and concept-id "
                "rendering as generic VWL template support."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_CONTEXT_PROJECT_ID,
            handler=_handle_context_project,
            description=(
                "Project candidate context fields into a compact payload using "
                "a workflow-supplied item-output requirement, aliases, "
                "exclusions, and size bounds."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_KR_MATERIALISATION_GUARD_ID,
            handler=_handle_kr_materialisation_guard,
            description=(
                "Validate an optional caller-supplied KR materialisation "
                "contract before concept writes and resolved relationship "
                "assertions. The action enforces only the generic guard schema."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_RESOLUTION_ID,
            handler=_handle_kr_relationship_resolution,
            description=(
                "Deterministically resolve KR relationship endpoint keys "
                "against verified concept materialisation results while "
                "preserving exact concept IDs."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_READBACK_ID,
            handler=_handle_kr_relationship_readback,
            description=(
                "Verify that canonical endpoint read-back contains the exact "
                "KR relationship tuple requested by the workflow."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_RELATIONSHIP_EFFECT_READBACK_ID,
            handler=_handle_relationship_effect_readback,
            description=(
                "Bind successful relationship mutation targets from the current "
                "workflow's tool history to exact canonical relation read-back "
                "hits, defaulting to one target unless multi-target mode is "
                "explicitly enabled."
            ),
            input_schema={
                "type": "object",
                "required": [
                    "mutation_tool_name",
                    "expected_source_id",
                    "expected_predicate_id",
                    "expected_relation_kind",
                    "readback_concept_id",
                    "readback_total_hits",
                    "readback_hits",
                ],
                "properties": {
                    "mutation_tool_name": {"type": "string"},
                    "expected_source_id": {"type": "string"},
                    "expected_predicate_id": {"type": "string"},
                    "expected_relation_kind": {"type": "string"},
                    "tool_invocations": {"type": "array"},
                    "relationship_effect_receipt": {"type": "object"},
                    "allow_multiple_targets": {"type": "boolean"},
                    "minimum_unique_targets": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 80,
                    },
                    "readback_concept_id": {"type": "string"},
                    "readback_total_hits": {"type": "integer"},
                    "readback_total_hits_is_lower_bound": {"type": "boolean"},
                    "readback_hits": {"type": "array"},
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": [
                    "relationship_effect_readback_verified",
                    "relationship_effect_readback_failure_code",
                    "relationship_effect_mutation_targets",
                    "represented_target_concept_id",
                    "verified_relationship",
                    "verified_relationships",
                ],
                "properties": {
                    "relationship_effect_readback_verified": {"type": "boolean"},
                    "relationship_effect_readback_failure_code": {
                        "type": ["string", "null"]
                    },
                    "relationship_effect_mutation_targets": {"type": "array"},
                    "represented_target_concept_id": {"type": ["string", "null"]},
                    "verified_relationship": {"type": ["object", "null"]},
                    "verified_relationships": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
            },
            side_effects="none",
            postconditions=("exact_relationship_effect_readback_correlated",),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_TEXT_EFFECT_READBACK_ID,
            handler=_handle_text_effect_readback,
            description=(
                "Bind successful text mutations from the current workflow to "
                "exact actor-effective canonical text-relation read-back. The "
                "workflow supplies the required predicates."
            ),
            input_schema={
                "type": "object",
                "required": ["required_predicates"],
                "properties": {
                    "required_predicates": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {"type": "string"},
                    },
                    "optional_predicates": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string"},
                    },
                    "tool_invocations": {"type": "array"},
                    "text_effect_receipts": {"type": "array"},
                    "expected_concept_id": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": [
                    "text_effect_readback_verified",
                    "text_effect_readback_failure_code",
                    "represented_concept_id",
                    "verified_text_effects",
                ],
                "properties": {
                    "text_effect_readback_verified": {"type": "boolean"},
                    "text_effect_readback_failure_code": {
                        "type": ["string", "null"]
                    },
                    "represented_concept_id": {"type": ["string", "null"]},
                    "verified_text_effects": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
            },
            side_effects="none",
            postconditions=("exact_text_effect_readback_correlated",),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_BREAK_ID,
            handler=_build_break_handler(),
            description="Emit loop break control signal for explicit on_break routing.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_CONTINUE_ID,
            handler=_build_continue_handler(),
            description=(
                "Emit loop continue control signal for explicit on_continue routing."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_PAUSE_AT_CHECKPOINT_ID,
            handler=_handle_pause_at_checkpoint,
            description=(
                "Request one cooperative durable pause after the current state "
                "has transitioned and the next-state checkpoint is durably saved. "
                "A later explicit resume continues the same instance."
            ),
            input_schema={
                "type": "object",
                "properties": {"reason_code": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": [WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY],
                "properties": {
                    WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY: {"type": "object"}
                },
            },
            side_effects="durable_execution_control",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_FORK_ID,
            handler=_build_fork_handler(registry=registry, definition_loader=loader),
            description=(
                "Execute independent child workflow branches with deterministic "
                "failure and merge policy semantics."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
            handler=_build_for_each_handler(
                registry=registry,
                definition_loader=loader,
            ),
            description=(
                "Execute one child workflow per item in a deterministic input "
                "sequence and collect structured per-item outcomes."
            ),
            required_tool_operation_class="workflow_execute",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CONTROL_ACTION_JOIN_ID,
            handler=_build_join_handler(),
            description="Join and merge results for a previously executed fork.",
        )
    )


__all__ = ["register_control_flow_actions"]
