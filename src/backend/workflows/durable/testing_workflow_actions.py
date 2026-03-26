"""Durable action handlers for Testing Workflows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...services.experiment_run_service import (
    compute_experiment_verdict,
    create_experiment_spec,
    emit_experiment_learning_signal,
    execute_regression_suite,
    prepare_experiment_spec_from_template,
    prepare_meeting_invitation_experiment_spec,
    record_experiment_observation,
    start_experiment_run,
)
from ...services.arxiv_ingestion_testing_service import (
    cleanup_arxiv_paper_ingestion_test_artifacts,
    prepare_arxiv_paper_ingestion_test_fixture,
    verify_arxiv_paper_ingestion_test_result,
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
    TESTING_CLEANUP_ARXIV_PAPER_INGESTION_ARTIFACTS_ACTION_ID,
    TESTING_PREPARE_ARXIV_PAPER_INGESTION_FIXTURE_ACTION_ID,
    TESTING_PREPARE_EXPERIMENT_SPEC_ACTION_ID,
    TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
    TESTING_VERIFY_ARXIV_PAPER_INGESTION_RESULT_ACTION_ID,
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
from .execution_observability import (
    await_workflow_terminal_state,
    build_workflow_execution_response,
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


def _compact_workflow_execution_payload(
    workflow_execution_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = (
        dict(workflow_execution_payload)
        if isinstance(workflow_execution_payload, Mapping)
        else {}
    )
    compact: dict[str, Any] = {}
    for key in (
        "workflow_id",
        "instance_id",
        "launch_mode",
        "await_terminal",
        "timeout_seconds",
        "poll_interval_seconds",
        "poll_count",
        "timed_out",
        "current_status",
        "current_state",
        "final_status",
        "error",
        "error_step",
        "execution_trace_id",
        "step_result_envelope_count",
    ):
        if key in raw:
            compact[key] = raw.get(key)

    outputs = raw.get("outputs")
    compact_outputs = _compact_workflow_execution_outputs(outputs)
    if compact_outputs:
        compact["outputs"] = compact_outputs

    workflow_result_envelope = raw.get("workflow_result_envelope")
    if isinstance(workflow_result_envelope, Mapping):
        compact["workflow_result_envelope"] = dict(workflow_result_envelope)

    metadata_validation = raw.get("metadata_validation")
    if isinstance(metadata_validation, Mapping):
        compact_metadata: dict[str, Any] = {}
        summary = metadata_validation.get("summary")
        if isinstance(summary, Mapping):
            compact_metadata["summary"] = dict(summary)
        last_event = metadata_validation.get("last_event")
        if isinstance(last_event, Mapping):
            compact_metadata["last_event"] = dict(last_event)
        if compact_metadata:
            compact["metadata_validation"] = compact_metadata

    return compact


_OMIT_WORKFLOW_OUTPUT_VALUE = object()


def _compact_workflow_execution_outputs(outputs: Any) -> dict[str, Any]:
    if not isinstance(outputs, Mapping):
        return {}

    compact: dict[str, Any] = {}
    omitted_keys: list[str] = []
    for raw_key, raw_value in outputs.items():
        key = _safe_str(raw_key)
        if not key:
            continue
        compacted = _compact_workflow_execution_output_value(raw_value)
        if compacted is _OMIT_WORKFLOW_OUTPUT_VALUE:
            omitted_keys.append(key)
            continue
        compact[key] = compacted
    if omitted_keys:
        compact["omitted_output_keys"] = omitted_keys
    return compact


def _compact_workflow_execution_output_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        return text[:2048] if len(text) > 2048 else text
    if isinstance(value, Mapping):
        return _OMIT_WORKFLOW_OUTPUT_VALUE
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return _OMIT_WORKFLOW_OUTPUT_VALUE

    compact_items: list[Any] = []
    omitted = False
    for item in list(value)[:25]:
        compacted = _compact_workflow_execution_output_value(item)
        if compacted is _OMIT_WORKFLOW_OUTPUT_VALUE:
            omitted = True
            continue
        compact_items.append(compacted)
    if len(value) > 25:
        omitted = True
    if not compact_items and not omitted:
        return []
    if compact_items:
        return compact_items
    return _OMIT_WORKFLOW_OUTPUT_VALUE


def _summarise_observation_recording_payload(
    observation_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(observation_payload) if isinstance(observation_payload, Mapping) else {}
    recorded = raw.get("recorded_observations")
    recorded_observations = (
        [dict(item) for item in recorded if isinstance(item, Mapping)]
        if isinstance(recorded, list)
        else []
    )
    experiment_run_raw = raw.get("experiment_run")
    aggregated_observations_raw = (
        experiment_run_raw.get("observations")
        if isinstance(experiment_run_raw, Mapping)
        else None
    )
    aggregated_observations = (
        [dict(item) for item in aggregated_observations_raw if isinstance(item, Mapping)]
        if isinstance(aggregated_observations_raw, list)
        else []
    )
    labels = [
        label
        for label in (
            _safe_str(item.get("label")) for item in recorded_observations
        )
        if label
    ]
    summary: dict[str, Any] = {
        "success": bool(raw.get("success")),
        "run_id": _safe_str(raw.get("run_id")) or None,
        "recorded_observation_count": len(recorded_observations),
    }
    if labels:
        summary["recorded_observation_labels"] = labels
    if aggregated_observations:
        summary["observation_count"] = len(aggregated_observations)
        summary["observations"] = aggregated_observations
    elif recorded_observations:
        summary["observation_count"] = len(recorded_observations)
        summary["observations"] = recorded_observations
    error = _safe_str(raw.get("error")) or _safe_str(raw.get("error_code")) or None
    if error:
        summary["error"] = error
    return summary


def _summarise_experiment_start_run_payload(
    start_run_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(start_run_payload) if isinstance(start_run_payload, Mapping) else {}
    experiment_run_raw = raw.get("experiment_run")
    experiment_run = (
        dict(experiment_run_raw) if isinstance(experiment_run_raw, Mapping) else {}
    )
    summary: dict[str, Any] = {
        "success": bool(raw.get("success")),
        "run_id": _safe_str(raw.get("run_id")) or None,
        "experiment_spec_id": _safe_str(experiment_run.get("experiment_spec_id")) or None,
        "status": _safe_str(experiment_run.get("status")) or None,
        "theory_id": _safe_str(experiment_run.get("theory_id")) or None,
    }
    for key in ("target_workflow_ids", "candidate_workflow_ids"):
        value = experiment_run.get(key)
        if isinstance(value, list) and value:
            summary[key] = list(value)
    benchmark_tier = _safe_str(experiment_run.get("benchmark_tier")) or None
    if benchmark_tier:
        summary["benchmark_tier"] = benchmark_tier
    error = _safe_str(raw.get("error")) or _safe_str(raw.get("error_code")) or None
    if error:
        summary["error"] = error
    return summary


def _summarise_experiment_verdict_payload(
    verdict_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(verdict_payload) if isinstance(verdict_payload, Mapping) else {}
    summary: dict[str, Any] = {
        "success": bool(raw.get("success")),
        "run_id": _safe_str(raw.get("run_id")) or None,
        "verdict": _safe_str(raw.get("verdict")) or None,
    }
    verdict_summary = raw.get("verdict_summary")
    if isinstance(verdict_summary, Mapping):
        summary["verdict_summary"] = dict(verdict_summary)
    promotion_recommendation = raw.get("promotion_recommendation")
    if isinstance(promotion_recommendation, Mapping):
        summary["promotion_recommendation"] = dict(promotion_recommendation)
    observations = raw.get("observations")
    if isinstance(observations, list):
        summary["observation_count"] = len(
            [item for item in observations if isinstance(item, Mapping)]
        )
    error = _safe_str(raw.get("error")) or _safe_str(raw.get("error_code")) or None
    if error:
        summary["error"] = error
    return summary


def _summarise_learning_signal_payload(
    learning_signal_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(learning_signal_payload) if isinstance(learning_signal_payload, Mapping) else {}
    learning_signal_raw = raw.get("learning_signal")
    learning_signal = (
        dict(learning_signal_raw) if isinstance(learning_signal_raw, Mapping) else {}
    )
    replay_case_raw = learning_signal.get("replay_case")
    replay_case = dict(replay_case_raw) if isinstance(replay_case_raw, Mapping) else {}
    summary: dict[str, Any] = {
        "success": bool(raw.get("success")),
        "run_id": _safe_str(raw.get("run_id")) or None,
        "learning_signal": {
            "schema_version": _safe_str(learning_signal.get("schema_version")) or None,
            "emitted_at_utc": _safe_str(learning_signal.get("emitted_at_utc")) or None,
            "selection_experience_id": _safe_str(
                learning_signal.get("selection_experience_id")
            )
            or None,
            "selection_outcome": _safe_str(learning_signal.get("selection_outcome"))
            or None,
            "selection_reward": learning_signal.get("selection_reward"),
            "replay_case": {
                "case_id": _safe_str(replay_case.get("case_id")) or None,
                "expected_workflow_id": _safe_str(
                    replay_case.get("expected_workflow_id")
                )
                or None,
                "baseline_workflow_id": _safe_str(
                    replay_case.get("baseline_workflow_id")
                )
                or None,
                "verdict": _safe_str(replay_case.get("verdict")) or None,
            },
        },
    }
    selection_experience = raw.get("selection_experience")
    if isinstance(selection_experience, Mapping):
        summary["selection_experience"] = dict(selection_experience)
    error = _safe_str(raw.get("error")) or _safe_str(raw.get("error_code")) or None
    if error:
        summary["error"] = error
    return summary


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
    return _result_from_payload(_summarise_experiment_start_run_payload(result))


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
    return _result_from_payload(_summarise_observation_recording_payload(result))


def _handle_experiment_compute_verdict(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    return _result_from_payload(
        _summarise_experiment_verdict_payload(
            compute_experiment_verdict(run_id=_safe_str(inputs.get("run_id")))
        )
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
    return _result_from_payload(_summarise_learning_signal_payload(result))


def _handle_experiment_execute_target_workflow(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    from . import WorkflowInstanceManager
    from .workflow_instance_submission_service import (
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
        max_retries=int(inputs.get("max_retries", 3) or 3),
    )
    payload = build_workflow_execution_response(
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

    wait_result = await_workflow_terminal_state(
        manager,
        instance_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    payload = build_workflow_execution_response(
        submission,
        workflow_inputs=workflow_inputs,
        instance=wait_result.instance,
        await_terminal=True,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        poll_count=wait_result.poll_count,
        timed_out=wait_result.timed_out,
    )
    workflow_execution = _compact_workflow_execution_payload(
        payload.get("workflow_execution")
    )
    payload["workflow_execution"] = workflow_execution
    payload.pop("workflow_instance", None)
    final_status = _safe_str(payload.get("final_status"))
    timed_out = bool(payload.get("timed_out"))
    poll_count = int(workflow_execution.get("poll_count") or 0)

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
        payload["observation_recording"] = _summarise_observation_recording_payload(
            observation_payload
        )
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
        suite_policy=inputs.get("suite_policy"),
    )
    return _result_from_payload(result)


def _handle_testing_prepare_experiment_spec(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    result = prepare_experiment_spec_from_template(
        scenario_template=inputs.get("scenario_template"),
        template_inputs={
            key: value
            for key, value in inputs.items()
            if key
            not in {
                "scenario_template",
                "experiment_spec_id",
                "name",
                "namespace",
                "user_id",
                "org_id",
            }
        },
        experiment_spec_id=_safe_str(inputs.get("experiment_spec_id")) or None,
        name=_safe_str(inputs.get("name")) or None,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
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


def _handle_testing_prepare_arxiv_fixture(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    result = prepare_arxiv_paper_ingestion_test_fixture(
        prompt_text=(
            _safe_str(inputs.get("prompt_text"))
            or _safe_str(inputs.get("prompt"))
            or None
        ),
        arxiv_source=_safe_str(inputs.get("arxiv_source")) or None,
        source_uri=_safe_str(inputs.get("source_uri")) or None,
        arxiv_id=_safe_str(inputs.get("arxiv_id")) or None,
        user_concept_id=actor["user_id"],
        timeout_seconds=_coerce_float(
            inputs.get("timeout_seconds"),
            default=15.0,
            minimum=1.0,
        ),
        repair_existing_artifacts=_coerce_bool(
            inputs.get("repair_existing_artifacts"),
            default=False,
        ),
    )
    return _result_from_payload(result)


def _handle_testing_verify_arxiv_ingestion_result(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    result = verify_arxiv_paper_ingestion_test_result(
        workflow_execution=inputs.get("workflow_execution"),
        arxiv_id=_safe_str(inputs.get("arxiv_id")) or None,
        source_uri=_safe_str(inputs.get("source_uri")) or None,
        expected_title=_safe_str(inputs.get("expected_title")) or None,
        expected_summary=_safe_str(inputs.get("expected_summary")) or None,
        expected_publication_date=(
            _safe_str(inputs.get("expected_publication_date")) or None
        ),
        expected_author_names=inputs.get("expected_author_names") or [],
        expected_author_concept_ids=inputs.get("expected_author_concept_ids") or [],
        expected_topic_labels=inputs.get("expected_topic_labels") or [],
        expected_topic_concept_ids=inputs.get("expected_topic_concept_ids") or [],
        paper_concept_id=_safe_str(inputs.get("paper_concept_id")) or None,
    )
    return _result_from_payload(result)


def _handle_testing_cleanup_arxiv_ingestion_artifacts(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    result = cleanup_arxiv_paper_ingestion_test_artifacts(
        paper_concept_id=_safe_str(inputs.get("paper_concept_id")) or None,
        file_copy_concept_id=_safe_str(inputs.get("file_copy_concept_id")) or None,
        file_copy_concept_ids=inputs.get("file_copy_concept_ids") or [],
        author_concept_ids=inputs.get("author_concept_ids") or [],
        topic_concept_ids=inputs.get("topic_concept_ids") or [],
        preexisting_author_concept_ids=(
            inputs.get("preexisting_author_concept_ids") or []
        ),
        preexisting_topic_concept_ids=(
            inputs.get("preexisting_topic_concept_ids") or []
        ),
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
            action_id=TESTING_PREPARE_EXPERIMENT_SPEC_ACTION_ID,
            handler=_handle_testing_prepare_experiment_spec,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
            handler=_handle_testing_prepare_meeting_spec,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_PREPARE_ARXIV_PAPER_INGESTION_FIXTURE_ACTION_ID,
            handler=_handle_testing_prepare_arxiv_fixture,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_VERIFY_ARXIV_PAPER_INGESTION_RESULT_ACTION_ID,
            handler=_handle_testing_verify_arxiv_ingestion_result,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_CLEANUP_ARXIV_PAPER_INGESTION_ARTIFACTS_ACTION_ID,
            handler=_handle_testing_cleanup_arxiv_ingestion_artifacts,
        )
    )


__all__ = ["register_testing_workflow_actions"]
