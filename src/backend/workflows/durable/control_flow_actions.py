"""Action handlers for first-class workflow control-flow primitives.

JVNAUTOSCI-1311:
- explicit break/continue step semantics for loop contexts,
- first-class fork/join semantics with deterministic merge and failure policy,
- shared runtime metrics/events for observability.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Mapping, Sequence

from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import WorkflowDefinition, WorkflowExecutor
from ..execution_contracts import (
    LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
    WORKFLOW_CONTROL_ACTION_BREAK_ID,
    WORKFLOW_CONTROL_ACTION_CONTINUE_ID,
    WORKFLOW_CONTROL_ACTION_FORK_ID,
    WORKFLOW_CONTROL_ACTION_JOIN_ID,
    WORKFLOW_CONTROL_SIGNAL_BREAK,
    WORKFLOW_CONTROL_SIGNAL_CONTINUE,
    WORKFLOW_FORK_ALLOWED_FAILURE_POLICIES,
    WORKFLOW_FORK_ALLOWED_MERGE_POLICIES,
    WORKFLOW_FORK_CONTEXTS_KEY,
    WORKFLOW_FORK_FAILURE_POLICY_COLLECT_ERRORS,
    WORKFLOW_FORK_FAILURE_POLICY_FAIL_FAST,
    WORKFLOW_FORK_MERGE_POLICY_LAST_WRITER_WINS,
    append_runtime_event,
    increment_runtime_metric,
)
from ..trace_model import WorkflowExecutionTrace
from ..vontology_loader import load_workflow_definition_from_vontology


_DEFAULT_FORK_BRANCH_LIMIT = 8
_MAX_FORK_BRANCH_LIMIT = 32
_DEFAULT_BRANCH_MAX_TRANSITIONS = 40
_MAX_BRANCH_MAX_TRANSITIONS = 200
_FORK_BRANCH_LIMIT_ENV = "VON_WORKFLOW_FORK_MAX_BRANCHES"
_FORK_BRANCH_TRANSITIONS_ENV = "VON_WORKFLOW_FORK_BRANCH_MAX_TRANSITIONS"


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
            child_definition = definition_loader(child_workflow_id)
            if child_definition is None:
                branch_result = {
                    "branch_id": branch["branch_id"],
                    "workflow_id": child_workflow_id,
                    "completed": False,
                    "final_state": "",
                    "error": f"fork_child_definition_not_found:{child_workflow_id}",
                    "result": {},
                    "result_envelope": None,
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


def register_control_flow_actions(
    registry: ActionRegistry,
    *,
    definition_loader: Callable[[str], WorkflowDefinition | None] | None = None,
) -> None:
    """Register break/continue/fork/join primitives in the action registry."""

    loader = definition_loader or load_workflow_definition_from_vontology
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
            action_id=WORKFLOW_CONTROL_ACTION_JOIN_ID,
            handler=_build_join_handler(),
            description="Join and merge results for a previously executed fork.",
        )
    )


__all__ = ["register_control_flow_actions"]
