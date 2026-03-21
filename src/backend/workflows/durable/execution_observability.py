"""Shared durable workflow execution observability helpers.

These helpers keep awaited execution, result projection, and trace summaries
aligned across workflow actions and MCP surfaces.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any

from ..execution_contracts import (
    LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
    WORKFLOW_RESULT_ENVELOPE_KEY,
    WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
)
from ..metadata_validation import LAST_METADATA_EVENT_KEY, WORKFLOW_METADATA_EVENTS_KEY
from ..trace_store import get_workflow_execution_trace
from .workflow_instance_submission_service import (
    WorkflowInstanceSubmissionResult,
    build_verified_instance_launch_payload,
)


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def normalise_workflow_status(value: Any) -> str:
    status_value = getattr(value, "value", value)
    return _safe_str(status_value).lower()


def is_terminal_workflow_status(value: Any) -> bool:
    return normalise_workflow_status(value) in {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class AwaitedWorkflowTerminalResult:
    instance: Any | None
    poll_count: int
    timed_out: bool

    @property
    def final_status(self) -> str | None:
        if self.instance is None:
            return None
        status = normalise_workflow_status(getattr(self.instance, "status", None))
        return status or None


def await_workflow_terminal_state(
    manager: Any,
    instance_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> AwaitedWorkflowTerminalResult:
    """Poll a durable instance until it becomes terminal or the timeout expires."""

    poll_count = 0
    latest_instance: Any | None = None
    deadline = monotonic() + max(0.0, float(timeout_seconds))

    while True:
        latest_instance = manager.get_instance(instance_id)
        poll_count += 1
        if latest_instance is not None and is_terminal_workflow_status(
            getattr(latest_instance, "status", None)
        ):
            return AwaitedWorkflowTerminalResult(
                instance=latest_instance,
                poll_count=poll_count,
                timed_out=False,
            )
        if monotonic() >= deadline:
            return AwaitedWorkflowTerminalResult(
                instance=latest_instance,
                poll_count=poll_count,
                timed_out=True,
            )
        sleep(max(0.0, float(poll_interval_seconds)))


def build_workflow_instance_payload(
    instance: Any,
    *,
    include_inputs: bool = True,
    include_outputs: bool = True,
    include_workflow_data: bool = False,
) -> dict[str, Any]:
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
        "error",
        "error_step",
        "user_id",
        "org_id",
        "namespace",
        "schedule_id",
        "execution_trace_id",
    ):
        if field in payload:
            continue
        value = getattr(instance, field, None)
        if value is not None:
            payload[field] = value

    if include_inputs and "inputs" not in payload:
        inputs = getattr(instance, "inputs", None)
        if isinstance(inputs, Mapping):
            payload["inputs"] = dict(inputs)
    if include_outputs and "outputs" not in payload:
        outputs = getattr(instance, "outputs", None)
        if isinstance(outputs, Mapping):
            payload["outputs"] = dict(outputs)
        elif outputs is not None:
            payload["outputs"] = outputs
    if include_workflow_data and "workflow_data" not in payload:
        workflow_data = getattr(instance, "workflow_data", None)
        if isinstance(workflow_data, Mapping):
            payload["workflow_data"] = dict(workflow_data)

    status = payload.get("status")
    if status is None:
        normalised_status = normalise_workflow_status(getattr(instance, "status", None))
        if normalised_status:
            payload["status"] = normalised_status

    execution_trace_id = _safe_str(payload.get("execution_trace_id"))
    payload["execution_trace_id"] = execution_trace_id or None
    return payload


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return items
    for item in value:
        if isinstance(item, Mapping):
            items.append(dict(item))
    return items


def build_metadata_validation_summary(
    events: Sequence[Mapping[str, Any]],
    *,
    last_event: Mapping[str, Any] | None,
) -> dict[str, Any]:
    failed_count = 0
    enforced_failed_count = 0
    skipped_count = 0
    applied_count = 0
    for event in events:
        if bool(event.get("applied")):
            applied_count += 1
        if bool(event.get("skipped")):
            skipped_count += 1
        if event.get("ok") is False:
            failed_count += 1
            if bool(event.get("enforced")):
                enforced_failed_count += 1

    last_payload = dict(last_event) if isinstance(last_event, Mapping) else None
    return {
        "event_count": len(events),
        "applied_count": applied_count,
        "failed_count": failed_count,
        "enforced_failed_count": enforced_failed_count,
        "skipped_count": skipped_count,
        "last_event_phase": _safe_str(
            last_payload.get("phase") if last_payload is not None else None
        )
        or None,
        "last_event_ok": (
            last_payload.get("ok") if last_payload is not None else None
        ),
        "last_reason_code": _safe_str(
            last_payload.get("reason_code") if last_payload is not None else None
        )
        or None,
    }


def build_workflow_execution_telemetry(
    instance: Any,
    *,
    include_step_result_envelopes: bool = False,
) -> dict[str, Any]:
    instance_payload = build_workflow_instance_payload(
        instance,
        include_inputs=False,
        include_outputs=False,
        include_workflow_data=False,
    )
    workflow_data = getattr(instance, "workflow_data", None)
    workflow_context = (
        dict(workflow_data) if isinstance(workflow_data, Mapping) else {}
    )
    step_result_envelopes = _mapping_list(
        workflow_context.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY)
    )
    latest_step_result_envelope = _mapping_or_none(
        workflow_context.get(LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY)
    )
    if latest_step_result_envelope is None and step_result_envelopes:
        latest_step_result_envelope = dict(step_result_envelopes[-1])

    metadata_events = _mapping_list(workflow_context.get(WORKFLOW_METADATA_EVENTS_KEY))
    last_metadata_event = _mapping_or_none(workflow_context.get(LAST_METADATA_EVENT_KEY))

    telemetry: dict[str, Any] = {
        "workflow_result_envelope": _mapping_or_none(
            workflow_context.get(WORKFLOW_RESULT_ENVELOPE_KEY)
        ),
        "latest_step_result_envelope": latest_step_result_envelope,
        "step_result_envelope_count": len(step_result_envelopes),
        "metadata_validation": {
            "summary": build_metadata_validation_summary(
                metadata_events,
                last_event=last_metadata_event,
            ),
            "last_event": last_metadata_event,
            "events": metadata_events,
        },
        "progress": dict(instance_payload.get("progress") or {}),
        "execution_trace_id": instance_payload.get("execution_trace_id"),
    }
    if include_step_result_envelopes:
        telemetry["step_result_envelopes"] = step_result_envelopes
    return telemetry


def build_workflow_execution_response(
    submission: WorkflowInstanceSubmissionResult,
    *,
    workflow_inputs: Mapping[str, Any] | None = None,
    instance: Any | None = None,
    await_terminal: bool = False,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
    poll_count: int | None = None,
    timed_out: bool = False,
    include_step_result_envelopes: bool = False,
    include_trace: bool = False,
) -> dict[str, Any]:
    payload = build_verified_instance_launch_payload(
        submission,
        workflow_inputs=workflow_inputs,
    )
    if not isinstance(payload.get("workflow_execution"), Mapping):
        return payload
    workflow_execution = (
        dict(payload.get("workflow_execution"))
        if isinstance(payload.get("workflow_execution"), Mapping)
        else {}
    )

    if await_terminal:
        workflow_execution["await_terminal"] = True
    if timeout_seconds is not None:
        workflow_execution["timeout_seconds"] = float(timeout_seconds)
    if poll_interval_seconds is not None:
        workflow_execution["poll_interval_seconds"] = float(poll_interval_seconds)
    if poll_count is not None:
        workflow_execution["poll_count"] = int(poll_count)
    if await_terminal or poll_count is not None or timeout_seconds is not None:
        workflow_execution["timed_out"] = bool(timed_out)

    final_status: str | None = None
    if instance is not None:
        instance_payload = build_workflow_instance_payload(instance)
        payload["workflow_instance"] = instance_payload
        final_status = _safe_str(instance_payload.get("status")) or None
        workflow_execution.update(
            {
                "current_status": final_status,
                "current_state": _safe_str(instance_payload.get("current_state"))
                or None,
                "outputs": instance_payload.get("outputs"),
                "error": _safe_str(instance_payload.get("error")) or None,
                "error_step": _safe_str(instance_payload.get("error_step")) or None,
            }
        )
        workflow_execution.update(
            build_workflow_execution_telemetry(
                instance,
                include_step_result_envelopes=include_step_result_envelopes,
            )
        )
        if final_status:
            workflow_execution["final_status"] = final_status
            payload["final_status"] = final_status
    if await_terminal or timed_out:
        payload["timed_out"] = bool(timed_out)

    trace_id = _safe_str(workflow_execution.get("execution_trace_id")) or None
    if include_trace:
        payload["execution_trace"] = (
            get_workflow_execution_trace(trace_id) if trace_id else None
        )

    payload["workflow_execution"] = workflow_execution
    return payload


def build_workflow_execution_trace_summary(
    trace_doc: Mapping[str, Any],
) -> dict[str, Any]:
    metadata = trace_doc.get("metadata")
    metadata_map = dict(metadata) if isinstance(metadata, Mapping) else {}
    state_transitions = _mapping_list(trace_doc.get("state_transitions"))
    actions = _mapping_list(trace_doc.get("actions"))
    steps = _mapping_list(trace_doc.get("steps"))
    failed_steps = [
        step
        for step in steps
        if _safe_str(step.get("status")).lower() == "failed"
    ]
    last_error = None
    for step in reversed(failed_steps):
        error = _safe_str(step.get("error"))
        if error:
            last_error = error
            break

    return {
        "execution_id": _safe_str(trace_doc.get("execution_id")) or None,
        "workflow_id": _safe_str(trace_doc.get("workflow_id")) or None,
        "instance_id": _safe_str(trace_doc.get("instance_id"))
        or _safe_str(metadata_map.get("instance_id"))
        or None,
        "status": _safe_str(trace_doc.get("status")) or None,
        "start_time": trace_doc.get("start_time"),
        "end_time": trace_doc.get("end_time"),
        "user_namespace": _safe_str(trace_doc.get("user_namespace")) or None,
        "org_id": _safe_str(trace_doc.get("org_id")) or None,
        "state_transition_count": len(state_transitions),
        "action_count": len(actions),
        "step_count": len(steps),
        "failed_step_count": len(failed_steps),
        "last_error": last_error,
    }
