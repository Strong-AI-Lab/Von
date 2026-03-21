"""Durable action handlers for Testing Workflows."""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic, sleep
from typing import Any

from ...services.experiment_run_service import (
    compute_experiment_verdict,
    create_experiment_spec,
    emit_experiment_learning_signal,
    execute_regression_suite,
    prepare_meeting_invitation_experiment_spec,
    record_experiment_observation,
    start_experiment_run,
)
from ...services.namespace_service import parse_namespace
from ...services.testing_theory_service import (
    assert_testing_theory_local_claims,
    compute_testing_theory_diff,
    create_testing_theory_slice,
    garbage_collect_expired_testing_theories,
    import_canonical_context_into_theory,
    promote_testing_theory_validated_claims,
    rollback_testing_theory_local_writes,
)
from ...services.testing_workflow_contracts import (
    EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
    EXPERIMENT_CREATE_SPEC_ACTION_ID,
    EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
    EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
    EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
    EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
    EXPERIMENT_START_RUN_ACTION_ID,
    TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
    THEORY_ASSERT_LOCAL_CLAIM_ACTION_ID,
    THEORY_COMPUTE_DIFF_ACTION_ID,
    THEORY_CREATE_SLICE_ACTION_ID,
    THEORY_GC_EXPIRED_SLICES_ACTION_ID,
    THEORY_IMPORT_CANONICAL_CONTEXT_ACTION_ID,
    THEORY_PROMOTE_VALIDATED_CLAIMS_ACTION_ID,
    THEORY_ROLLBACK_LOCAL_WRITES_ACTION_ID,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_float(
    value: Any,
    *,
    default: float,
    minimum: float = 0.0,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _context_mapping(request: WorkflowActionRequest) -> dict[str, Any]:
    return dict(request.data) if isinstance(request.data, dict) else {}


def _derive_actor_context(request: WorkflowActionRequest) -> dict[str, Any]:
    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}
    context = _context_mapping(request)
    namespace = _safe_str(inputs.get("namespace")) or _safe_str(context.get("namespace"))
    user_id = _safe_str(inputs.get("user_id")) or _safe_str(context.get("user_id"))
    org_id = _safe_str(inputs.get("org_id")) or _safe_str(context.get("org_id"))

    environment_namespace = _safe_str(getattr(request.environment, "user_namespace", None))
    if not namespace and environment_namespace:
        namespace = environment_namespace
    if namespace and (not user_id or not org_id):
        try:
            parsed = parse_namespace(namespace)
        except ValueError:
            parsed = {}
        user_slug = _safe_str(parsed.get("user_id"))
        org_slug = _safe_str(parsed.get("org_id"))
        if user_slug and not user_id:
            user_id = f"#V#{user_slug}"
        if org_slug and not org_id:
            org_id = f"#V#{org_slug}"

    return {
        "namespace": namespace or None,
        "user_id": user_id or None,
        "org_id": org_id or None,
    }


def _result_from_payload(payload: Mapping[str, Any] | None) -> WorkflowActionResult:
    data = dict(payload) if isinstance(payload, Mapping) else {}
    success = bool(data.get("success"))
    error = _safe_str(data.get("error")) or _safe_str(data.get("error_code")) or None
    return WorkflowActionResult(
        status="success" if success else "failed",
        outputs=data,
        error=None if success else error,
    )


def _workflow_execution_verdict_for_status(
    *,
    final_status: str,
    timed_out: bool,
) -> str:
    if timed_out or final_status in {"pending", "running", "paused", ""}:
        return "inconclusive"
    if final_status == "completed":
        return "pass"
    if final_status in {"failed", "cancelled"}:
        return "fail"
    return "inconclusive"


def _workflow_instance_payload(instance: Any) -> dict[str, Any]:
    payload = (
        instance.to_status_dict()
        if hasattr(instance, "to_status_dict")
        and callable(getattr(instance, "to_status_dict"))
        else {}
    )
    if not isinstance(payload, dict):
        payload = {}
    for field in (
        "instance_id",
        "workflow_id",
        "current_state",
        "inputs",
        "outputs",
        "error",
        "error_step",
        "user_id",
        "org_id",
        "namespace",
        "workflow_data",
    ):
        if field in payload:
            continue
        value = getattr(instance, field, None)
        if value is not None:
            payload[field] = value

    status = payload.get("status")
    if status is None:
        status_value = getattr(getattr(instance, "status", None), "value", None)
        if isinstance(status_value, str) and status_value.strip():
            payload["status"] = status_value.strip()
    return payload


def _handle_theory_create_slice(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    result = create_testing_theory_slice(
        name=_safe_str(inputs.get("name")) or "Ephemeral testing theory",
        theory_id=_safe_str(inputs.get("theory_id")) or None,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
        included_canonical_concept_ids=inputs.get("included_canonical_concept_ids") or [],
        included_theory_ids=inputs.get("included_theory_ids") or [],
        expected_observations=inputs.get("expected_observations") or [],
        promotion_policy=inputs.get("promotion_policy"),
        retention_policy=inputs.get("retention_policy"),
        experiment_spec_id=_safe_str(inputs.get("experiment_spec_id")) or None,
        ttl_seconds=inputs.get("ttl_seconds"),
        description=_safe_str(inputs.get("description")) or None,
    )
    return _result_from_payload(result)


def _handle_theory_import_context(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    result = import_canonical_context_into_theory(
        theory_id=_safe_str(inputs.get("theory_id")),
        concept_ids=inputs.get("concept_ids") or [],
        theory_ids=inputs.get("theory_ids") or [],
    )
    return _result_from_payload(result)


def _handle_theory_assert_local_claim(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    claims = inputs.get("claims")
    if claims is None and "claim" in inputs:
        claims = inputs.get("claim")
    result = assert_testing_theory_local_claims(
        theory_id=_safe_str(inputs.get("theory_id")),
        claims=claims or [],
    )
    return _result_from_payload(result)


def _handle_theory_compute_diff(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    return _result_from_payload(
        compute_testing_theory_diff(theory_id=_safe_str(inputs.get("theory_id")))
    )


def _handle_theory_rollback(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    result = rollback_testing_theory_local_writes(
        theory_id=_safe_str(inputs.get("theory_id")),
        assertion_ids=inputs.get("assertion_ids") or [],
        clear_all=bool(inputs.get("clear_all", False)),
    )
    return _result_from_payload(result)


def _handle_theory_promote(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    result = promote_testing_theory_validated_claims(
        theory_id=_safe_str(inputs.get("theory_id")),
        assertion_ids=inputs.get("assertion_ids") or [],
        experiment_run_id=_safe_str(inputs.get("experiment_run_id")) or None,
        required_verdict=_safe_str(inputs.get("required_verdict")) or "pass",
    )
    return _result_from_payload(result)


def _handle_theory_gc(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    result = garbage_collect_expired_testing_theories(
        now_utc=_safe_str(inputs.get("now_utc")) or None,
        limit=int(inputs.get("limit", 100) or 100),
    )
    return _result_from_payload(result)


def _handle_experiment_create_spec(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    result = create_experiment_spec(
        name=_safe_str(inputs.get("name")) or "Testing experiment",
        experiment_spec_id=_safe_str(inputs.get("experiment_spec_id")) or None,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
        description=_safe_str(inputs.get("description")) or None,
        target_workflow_ids=inputs.get("target_workflow_ids") or [],
        target_capability_ids=inputs.get("target_capability_ids") or [],
        candidate_workflow_ids=inputs.get("candidate_workflow_ids") or [],
        theory_id=_safe_str(inputs.get("theory_id")) or None,
        experiment_suite_id=_safe_str(inputs.get("experiment_suite_id")) or None,
        fixture_payload=inputs.get("fixture_payload"),
        theory_setup=inputs.get("theory_setup"),
        expected_outcomes=inputs.get("expected_outcomes") or [],
        allowed_side_effects=inputs.get("allowed_side_effects") or [],
        forbidden_side_effects=inputs.get("forbidden_side_effects") or [],
        verdict_rules=inputs.get("verdict_rules"),
        replay_policy=inputs.get("replay_policy"),
        promotion_policy=inputs.get("promotion_policy"),
        metadata=inputs.get("metadata"),
    )
    return _result_from_payload(result)


def _handle_experiment_start_run(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    result = start_experiment_run(
        experiment_spec_id=_safe_str(inputs.get("experiment_spec_id")),
        run_id=_safe_str(inputs.get("run_id")) or None,
        theory_id=_safe_str(inputs.get("theory_id")) or None,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
        target_workflow_ids=inputs.get("target_workflow_ids") or [],
        candidate_workflow_ids=inputs.get("candidate_workflow_ids") or [],
        benchmark_tier=_safe_str(inputs.get("benchmark_tier")) or None,
        benchmark_world_id=_safe_str(inputs.get("benchmark_world_id")) or None,
        selection_experience_id=_safe_str(inputs.get("selection_experience_id")) or None,
        turn_execution_request_ids=inputs.get("turn_execution_request_ids") or [],
        metadata=inputs.get("metadata"),
    )
    return _result_from_payload(result)


def _handle_experiment_record_observation(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    observations = inputs.get("observations")
    if observations is None and "observation" in inputs:
        observations = inputs.get("observation")
    result = record_experiment_observation(
        run_id=_safe_str(inputs.get("run_id")),
        observations=observations or [],
        turn_execution_request_ids=inputs.get("turn_execution_request_ids") or [],
    )
    return _result_from_payload(result)


def _handle_experiment_compute_verdict(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    return _result_from_payload(
        compute_experiment_verdict(run_id=_safe_str(inputs.get("run_id")))
    )


def _handle_experiment_emit_learning_signal(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    result = emit_experiment_learning_signal(
        run_id=_safe_str(inputs.get("run_id")),
        selection_experience_id=_safe_str(inputs.get("selection_experience_id")) or None,
        turn_text=_safe_str(inputs.get("turn_text")) or None,
        expected_workflow_id=_safe_str(inputs.get("expected_workflow_id")) or None,
        baseline_workflow_id=_safe_str(inputs.get("baseline_workflow_id")) or None,
    )
    return _result_from_payload(result)


def _handle_experiment_execute_target_workflow(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    from . import WorkflowInstanceManager
    from .workflow_instance_submission_service import (
        build_verified_instance_launch_payload,
        submit_verified_workflow_instance,
    )

    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    workflow_id = _safe_str(inputs.get("workflow_id")) or _safe_str(
        next(iter(inputs.get("target_workflow_ids") or []), None)
    )
    if not workflow_id:
        return WorkflowActionResult(
            status="failed",
            error="workflow_id_required",
            outputs={"success": False, "error": "workflow_id_required"},
        )

    workflow_inputs_raw = inputs.get("workflow_inputs")
    workflow_inputs: dict[str, Any] = (
        {
            str(key): value
            for key, value in workflow_inputs_raw.items()
            if isinstance(key, str) and key.strip()
        }
        if isinstance(workflow_inputs_raw, Mapping)
        else {}
    )
    run_id = _safe_str(inputs.get("run_id")) or None
    theory_id = _safe_str(inputs.get("theory_id")) or None
    await_terminal = _coerce_bool(inputs.get("await_terminal"), default=False)
    timeout_seconds = _coerce_float(
        inputs.get("timeout_seconds"),
        default=60.0 if await_terminal else 0.0,
        minimum=0.0,
    )
    poll_interval_seconds = _coerce_float(
        inputs.get("poll_interval_seconds"),
        default=1.0,
        minimum=0.0,
    )
    record_observation = _coerce_bool(
        inputs.get("record_observation"),
        default=await_terminal and run_id is not None,
    )
    observation_label = (
        _safe_str(inputs.get("observation_label")) or "target_workflow_execution"
    )
    expected_final_status = (
        _safe_str(inputs.get("expected_final_status")) or "completed"
        if await_terminal
        else None
    )
    if run_id:
        workflow_inputs.setdefault("experiment_run_id", run_id)
    if theory_id:
        workflow_inputs.setdefault("testing_theory_id", theory_id)

    manager = WorkflowInstanceManager()
    submission = submit_verified_workflow_instance(
        manager=manager,
        workflow_id=workflow_id,
        user_id=actor["user_id"] or "anonymous",
        org_id=actor["org_id"] or "default",
        namespace=actor["namespace"] or "#V#anonymous@default",
        inputs=workflow_inputs,
        max_retries=int(inputs.get("max_retries", 1) or 1),
    )
    payload = build_verified_instance_launch_payload(
        submission,
        workflow_inputs=workflow_inputs,
    )
    instance_id = _safe_str(payload.get("instance_id"))
    if not submission.success or not instance_id:
        return WorkflowActionResult(
            status="failed",
            error=_safe_str(payload.get("error")) or "workflow_submission_failed",
            outputs=payload,
        )

    if not await_terminal:
        return WorkflowActionResult(status="success", outputs=payload)

    poll_count = 0
    latest_instance: Any | None = None
    timed_out = False
    deadline = monotonic() + timeout_seconds if timeout_seconds > 0 else monotonic()
    while True:
        latest_instance = manager.get_instance(instance_id)
        poll_count += 1
        latest_status = getattr(latest_instance, "status", None)
        if latest_status is not None and bool(latest_status.is_terminal()):
            break
        if monotonic() >= deadline:
            timed_out = True
            break
        sleep(poll_interval_seconds)

    instance_payload = (
        _workflow_instance_payload(latest_instance)
        if latest_instance is not None
        else {"instance_id": instance_id, "workflow_id": workflow_id}
    )
    final_status = _safe_str(instance_payload.get("status"))
    workflow_execution = dict(payload["workflow_execution"])
    workflow_execution.update(
        {
            "await_terminal": True,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "poll_count": poll_count,
            "timed_out": timed_out,
            "final_status": final_status or None,
            "current_state": _safe_str(instance_payload.get("current_state")) or None,
            "outputs": instance_payload.get("outputs"),
            "error": _safe_str(instance_payload.get("error")) or None,
            "error_step": _safe_str(instance_payload.get("error_step")) or None,
        }
    )
    payload["workflow_instance"] = instance_payload
    payload["workflow_execution"] = workflow_execution
    payload["final_status"] = final_status or None
    payload["timed_out"] = timed_out

    if record_observation and run_id:
        verdict = _workflow_execution_verdict_for_status(
            final_status=final_status,
            timed_out=timed_out,
        )
        observation_payload = record_experiment_observation(
            run_id=run_id,
            observations=[
                {
                    "label": observation_label,
                    "verdict": verdict,
                    "expected_outcome": expected_final_status,
                    "observed_outcome": final_status or "unknown",
                    "matched_expected_outcome": (
                        final_status == expected_final_status
                        if expected_final_status
                        else None
                    ),
                    "workflow_execution": workflow_execution,
                    "metrics": {
                        "await_terminal": True,
                        "timeout_seconds": timeout_seconds,
                        "poll_interval_seconds": poll_interval_seconds,
                        "poll_count": poll_count,
                        "timed_out": timed_out,
                    },
                }
            ],
        )
        payload["observation_recording"] = observation_payload
        if not observation_payload.get("success"):
            return WorkflowActionResult(
                status="failed",
                outputs=payload,
                error=_safe_str(observation_payload.get("error"))
                or "experiment_observation_record_failed",
            )

    return WorkflowActionResult(status="success", outputs=payload)


def _handle_experiment_execute_regression_suite(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    result = execute_regression_suite(
        execution_tier=_safe_str(inputs.get("execution_tier")) or "tier1",
        cases=inputs.get("cases") or [],
        benchmark_scenario=inputs.get("benchmark_scenario"),
        output_root=_safe_str(inputs.get("output_root")) or None,
        run_id=_safe_str(inputs.get("run_id")) or None,
    )
    return _result_from_payload(result)


def _handle_testing_prepare_meeting_spec(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    result = prepare_meeting_invitation_experiment_spec(
        invitation_text=_safe_str(inputs.get("invitation_text")),
        experiment_spec_id=_safe_str(inputs.get("experiment_spec_id")) or None,
        name=_safe_str(inputs.get("name")) or None,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
        candidate_workflow_ids=inputs.get("candidate_workflow_ids") or [],
        expected_meeting_type=_safe_str(inputs.get("expected_meeting_type")) or None,
        expected_structure_fields=inputs.get("expected_structure_fields") or [],
        expected_downstream_actions=inputs.get("expected_downstream_actions") or [],
    )
    return _result_from_payload(result)


def register_testing_workflow_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(action_id=THEORY_CREATE_SLICE_ACTION_ID, handler=_handle_theory_create_slice)
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=THEORY_IMPORT_CANONICAL_CONTEXT_ACTION_ID,
            handler=_handle_theory_import_context,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=THEORY_ASSERT_LOCAL_CLAIM_ACTION_ID,
            handler=_handle_theory_assert_local_claim,
        )
    )
    registry.register_if_absent(
        ActionSpec(action_id=THEORY_COMPUTE_DIFF_ACTION_ID, handler=_handle_theory_compute_diff)
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=THEORY_ROLLBACK_LOCAL_WRITES_ACTION_ID,
            handler=_handle_theory_rollback,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=THEORY_PROMOTE_VALIDATED_CLAIMS_ACTION_ID,
            handler=_handle_theory_promote,
        )
    )
    registry.register_if_absent(
        ActionSpec(action_id=THEORY_GC_EXPIRED_SLICES_ACTION_ID, handler=_handle_theory_gc)
    )
    registry.register_if_absent(
        ActionSpec(action_id=EXPERIMENT_CREATE_SPEC_ACTION_ID, handler=_handle_experiment_create_spec)
    )
    registry.register_if_absent(
        ActionSpec(action_id=EXPERIMENT_START_RUN_ACTION_ID, handler=_handle_experiment_start_run)
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
            handler=_handle_experiment_record_observation,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
            handler=_handle_experiment_compute_verdict,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
            handler=_handle_experiment_emit_learning_signal,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
            handler=_handle_experiment_execute_target_workflow,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
            handler=_handle_experiment_execute_regression_suite,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
            handler=_handle_testing_prepare_meeting_spec,
        )
    )


__all__ = ["register_testing_workflow_actions"]
