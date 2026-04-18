"""Shared turn-execution support for orchestrator and durable runtimes.

Keep the supervising turn workflow's critic/completion-gate logic available to
both the interactive orchestrator and the durable action registry so runnable
verification and runtime behaviour stay in sync.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping, Sequence

from ...services.python_decision_authority_service import (
    annotate_python_decision_event,
)
from ...services.turn_execution_record_service import build_turn_execution_record
from ..action_registry import WorkflowActionResult
from ..definitions import (
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_non_negative_int(
    value: Any,
    *,
    default: int = 0,
    max_value: int | None = None,
) -> int:
    try:
        coerced = int(value)
    except Exception:
        coerced = default
    if coerced < 0:
        coerced = default
    if isinstance(max_value, int) and coerced > max_value:
        coerced = max_value
    return coerced


def _normalise_string_list(raw_values: Any) -> list[str]:
    normalised: list[str] = []
    if not isinstance(raw_values, list):
        return normalised
    for item in raw_values:
        cleaned = _safe_str(item)
        if cleaned:
            normalised.append(cleaned)
    return normalised


def _coerce_non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _looks_like_machine_json_text(text: Any) -> bool:
    candidate = _coerce_non_empty_text(text)
    if not candidate:
        return False
    if not (
        (candidate.startswith("{") and candidate.endswith("}"))
        or (candidate.startswith("[") and candidate.endswith("]"))
    ):
        return False
    try:
        parsed = json.loads(candidate)
    except Exception:
        return False
    return isinstance(parsed, (list, dict))


def _looks_like_execution_bookkeeping_response(
    text: Any,
    *,
    workflow_id: str | None = None,
) -> bool:
    candidate = _coerce_non_empty_text(text)
    if not candidate:
        return False

    lowered = candidate.lower()
    if lowered.startswith("the workflow completed successfully"):
        return True
    if lowered.startswith("the workflow finished successfully"):
        return True
    if lowered.startswith("workflow completed successfully"):
        return True

    if workflow_id:
        workflow_text = workflow_id.strip()
        if workflow_text and candidate == (
            f"Workflow {workflow_text} completed (state: completed)."
        ):
            return True
        if workflow_text and candidate.startswith(f"Workflow {workflow_text} "):
            if " completed (state: " in candidate or " failed (state: " in candidate:
                return True

    if candidate.startswith("Workflow ") and (
        " completed (state: " in candidate or " failed (state: " in candidate
    ):
        return True

    return False


def _render_structured_observation_lines(observations: Any) -> list[str]:
    if not isinstance(observations, Sequence) or isinstance(
        observations, (str, bytes, bytearray)
    ):
        return []

    lines: list[str] = []
    for item in observations[:6]:
        if isinstance(item, Mapping):
            label = _coerce_non_empty_text(item.get("label")) or "observation"
            verdict = _coerce_non_empty_text(item.get("verdict"))
            expected = _coerce_non_empty_text(item.get("expected_outcome"))
            observed = _coerce_non_empty_text(item.get("observed_outcome"))
            segments = [label]
            if verdict:
                segments.append(verdict)
            detail_parts: list[str] = []
            if expected:
                detail_parts.append(f"expected: {expected}")
            if observed:
                detail_parts.append(f"observed: {observed}")
            line = ": ".join(segments) if len(segments) > 1 else segments[0]
            if detail_parts:
                line = f"{line} ({'; '.join(detail_parts)})"
            lines.append(line)
            continue

        text = _coerce_non_empty_text(item)
        if text:
            lines.append(text)

    extra_count = len(observations) - len(lines)
    if extra_count > 0:
        lines.append(f"... (+{extra_count} more observations)")
    return lines


def _render_selected_workflow_artefact_lines(data: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    paper_concept_id = _coerce_non_empty_text(data.get("paper_concept_id"))
    file_copy_concept_id = _coerce_non_empty_text(data.get("file_copy_concept_id"))
    if paper_concept_id:
        lines.append(f"Created paper concept: {paper_concept_id}.")
    if file_copy_concept_id:
        lines.append(f"Linked file copy: {file_copy_concept_id}.")

    workflow_execution_summary = data.get("workflow_execution_summary")
    durable_side_effects = (
        workflow_execution_summary.get("durable_side_effects")
        if isinstance(workflow_execution_summary, Mapping)
        else data.get("durable_side_effects")
    )
    if isinstance(durable_side_effects, list):
        for item in durable_side_effects[:3]:
            if not isinstance(item, Mapping):
                continue
            mutation_kind = _coerce_non_empty_text(item.get("mutation_kind"))
            artefact_type = _coerce_non_empty_text(item.get("artefact_type"))
            artefact_ids = item.get("artefact_ids")
            if (
                mutation_kind == "created"
                and artefact_type
                and isinstance(artefact_ids, list)
                and artefact_ids
            ):
                first_id = _coerce_non_empty_text(artefact_ids[0])
                if first_id and first_id not in {
                    paper_concept_id,
                    file_copy_concept_id,
                }:
                    lines.append(
                        f"Created {artefact_type.replace('_', ' ')}: {first_id}."
                    )
    return lines


def _bounded_snapshot(
    value: Any,
    *,
    max_depth: int = 3,
    max_items: int = 6,
    max_string_length: int = 320,
    _depth: int = 0,
) -> Any:
    if _depth >= max_depth:
        if isinstance(value, Mapping):
            return {"_truncated": "mapping"}
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return ["_truncated"]
        text = value if isinstance(value, str) else repr(value)
        return (
            text[:max_string_length] + "..."
            if isinstance(text, str) and len(text) > max_string_length
            else text
        )

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return (
            value[:max_string_length] + "..."
            if len(value) > max_string_length
            else value
        )

    if isinstance(value, Mapping):
        bounded: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                continue
            if count >= max_items:
                bounded["_truncated_items"] = max(0, len(value) - max_items)
                break
            bounded[key] = _bounded_snapshot(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
            count += 1
        return bounded

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value[:max_items]) if not isinstance(value, list) else value[:max_items]
        bounded_items = [
            _bounded_snapshot(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
            for item in items
        ]
        if len(value) > max_items:
            bounded_items.append(f"... (+{len(value) - max_items} more)")
        return bounded_items

    text = repr(value)
    return text[:max_string_length] + "..." if len(text) > max_string_length else text


def _summarise_tool_batch_result_payload(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        if payload.get("success") is False:
            error_text = _coerce_non_empty_text(
                payload.get("error")
                or payload.get("error_code")
                or payload.get("message")
            )
            return f"Error: {error_text}" if error_text else "Error"
        for field_name in ("response_text", "final_response", "summary", "message"):
            candidate = _coerce_non_empty_text(payload.get(field_name))
            if candidate and not _looks_like_machine_json_text(candidate):
                return candidate
        concept_id = _coerce_non_empty_text(payload.get("concept_id"))
        if concept_id:
            return concept_id
        results = payload.get("results")
        if isinstance(results, list):
            return (
                f"{len(results)} result{'s' if len(results) != 1 else ''}"
                if results
                else None
            )
        items = payload.get("items")
        if isinstance(items, list):
            return (
                f"{len(items)} item{'s' if len(items) != 1 else ''}"
                if items
                else None
            )
    if isinstance(payload, list):
        return (
            f"{len(payload)} item{'s' if len(payload) != 1 else ''}"
            if payload
            else None
        )
    return None


def _derive_tool_batch_user_response(
    invocation_records: Sequence[Mapping[str, Any]],
) -> str | None:
    candidate_texts: list[str] = []
    seen_candidates: set[str] = set()

    for invocation in invocation_records[:6]:
        if not isinstance(invocation, Mapping):
            continue
        result_preview = invocation.get("result_preview")
        if isinstance(result_preview, Mapping):
            for field_name in ("response_text", "final_response", "summary", "message"):
                candidate = _coerce_non_empty_text(result_preview.get(field_name))
                if not candidate or _looks_like_machine_json_text(candidate):
                    continue
                lowered = candidate.lower()
                if lowered in seen_candidates:
                    continue
                seen_candidates.add(lowered)
                candidate_texts.append(candidate)
        result_summary = _coerce_non_empty_text(invocation.get("result_summary"))
        if result_summary and not result_summary.lower().startswith("error:"):
            lowered = result_summary.lower()
            if lowered not in seen_candidates:
                seen_candidates.add(lowered)
                candidate_texts.append(result_summary)

    if not candidate_texts:
        return None
    if len(candidate_texts) == 1:
        return candidate_texts[0]
    return "\n".join(candidate_texts[:3])


def _render_tool_batch_message_content(
    invocation_record: Mapping[str, Any],
) -> str:
    tool_name = _coerce_non_empty_text(invocation_record.get("tool")) or "tool"
    lines = [f"Tool: {tool_name}"]
    error_text = _coerce_non_empty_text(invocation_record.get("error"))
    result_summary = _coerce_non_empty_text(invocation_record.get("result_summary"))
    payload = invocation_record.get("payload")
    result_preview = invocation_record.get("result_preview")

    if isinstance(payload, Mapping):
        payload_text = json.dumps(
            _bounded_snapshot(payload),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        lines.append(f"Arguments: {payload_text}")
    if result_summary:
        lines.append(f"Result: {result_summary}")
    elif error_text:
        lines.append(f"Error: {error_text}")
    if result_preview is not None and not result_summary:
        preview_text = json.dumps(
            _bounded_snapshot(result_preview),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        lines.append(f"Output: {preview_text}")

    content = "\n".join(lines)
    return content[:1200] + "..." if len(content) > 1200 else content


def render_selected_workflow_user_response(
    *,
    selected_workflow_id: str | None,
    child_completed: bool,
    final_state: str | None,
    failure_detail: str | None,
    child_outputs: Mapping[str, Any] | None,
    child_result_snapshot: Mapping[str, Any] | None = None,
) -> str | None:
    """Derive a user-facing response from a selected-workflow outcome.

    This is intentionally separate from execution bookkeeping. Generic
    workflow-status summaries are not treated as answers.
    """

    child_outputs_map = (
        dict(child_outputs) if isinstance(child_outputs, Mapping) else {}
    )
    child_snapshot = (
        dict(child_result_snapshot)
        if isinstance(child_result_snapshot, Mapping)
        else {}
    )
    orchestrator_result = child_outputs_map.get("orchestrator_result")
    orchestrator_result_map = (
        dict(orchestrator_result) if isinstance(orchestrator_result, Mapping) else {}
    )
    completion_report = child_outputs_map.get("completion_report")
    completion_report_map = (
        dict(completion_report) if isinstance(completion_report, Mapping) else {}
    )
    workflow_execution_summary = child_outputs_map.get("workflow_execution_summary")
    workflow_execution_summary_map = (
        dict(workflow_execution_summary)
        if isinstance(workflow_execution_summary, Mapping)
        else {}
    )

    combined_data: dict[str, Any] = {}
    for payload in (
        workflow_execution_summary_map,
        completion_report_map,
        child_snapshot,
        orchestrator_result_map,
        child_outputs_map,
    ):
        if isinstance(payload, Mapping):
            for key, value in payload.items():
                if isinstance(key, str) and key not in combined_data:
                    combined_data[key] = value

    artefact_lines = _render_selected_workflow_artefact_lines(combined_data)
    failure_text = _coerce_non_empty_text(failure_detail) if not child_completed else None
    if failure_text and (
        any(char.isspace() for char in failure_text) or len(failure_text) > 120
    ):
        if artefact_lines:
            return "\n".join([*artefact_lines, "", failure_text])
        return failure_text

    candidate_texts: list[str] = []
    seen_candidates: set[str] = set()
    for payload, field_name in (
        (child_outputs_map, "response_text"),
        (child_outputs_map, "final_response"),
        (child_outputs_map, "current_response"),
        (child_outputs_map, "summary"),
        (orchestrator_result_map, "response_text"),
        (orchestrator_result_map, "final_response"),
        (orchestrator_result_map, "summary"),
        (child_snapshot, "response_text"),
        (child_snapshot, "final_response"),
        (completion_report_map, "response_text"),
        (workflow_execution_summary_map, "response_text"),
    ):
        candidate = _coerce_non_empty_text(payload.get(field_name))
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered in seen_candidates:
            continue
        seen_candidates.add(lowered)
        candidate_texts.append(candidate)

    for candidate_text in candidate_texts:
        if _looks_like_machine_json_text(candidate_text):
            continue
        if _looks_like_execution_bookkeeping_response(
            candidate_text,
            workflow_id=selected_workflow_id,
        ):
            continue
        if artefact_lines:
            return "\n".join([*artefact_lines, "", candidate_text])
        return candidate_text

    if failure_text:
        return failure_text

    lines: list[str] = list(artefact_lines)

    verdict = _coerce_non_empty_text(combined_data.get("verdict"))
    if verdict:
        lines.append(f"Workflow verdict: {verdict}.")

    run_id = _coerce_non_empty_text(combined_data.get("run_id"))
    if run_id:
        lines.append(f"Experiment run: {run_id}.")

    verdict_summary = combined_data.get("verdict_summary")
    if isinstance(verdict_summary, Mapping):
        summary_reason = _coerce_non_empty_text(verdict_summary.get("reason"))
        if summary_reason:
            lines.append(f"Verdict summary: {summary_reason}.")

    promotion = combined_data.get("promotion_recommendation")
    if isinstance(promotion, Mapping):
        promotion_bits: list[str] = []
        recommended = promotion.get("recommended")
        if isinstance(recommended, bool):
            promotion_bits.append("recommended" if recommended else "not recommended")
        requires_gate = promotion.get("requires_promotion_gate")
        if isinstance(requires_gate, bool):
            promotion_bits.append(
                "promotion gate required"
                if requires_gate
                else "no promotion gate required"
            )
        promotion_reason = _coerce_non_empty_text(promotion.get("reason"))
        if promotion_reason:
            promotion_bits.append(promotion_reason)
        if promotion_bits:
            lines.append(f"Promotion recommendation: {'; '.join(promotion_bits)}.")

    meeting_type = _coerce_non_empty_text(combined_data.get("candidate_meeting_type"))
    if meeting_type:
        lines.append(f"Candidate meeting type: {meeting_type}.")

    safe_downstream_action = _coerce_non_empty_text(
        combined_data.get("candidate_safe_downstream_action")
    )
    if safe_downstream_action:
        lines.append(f"Candidate safe downstream action: {safe_downstream_action}.")

    observation_lines = _render_structured_observation_lines(
        combined_data.get("meeting_candidate_observations")
        or combined_data.get("observations")
    )
    if observation_lines:
        lines.append("Evidence:")
        lines.extend(f"- {line}" for line in observation_lines)

    if lines:
        return "\n".join(lines)

    return None


def build_turn_execution_selected_workflow_outputs(
    *,
    selected_workflow_id: str | None,
    child_completed: bool,
    final_state: str | None,
    failure_detail: str | None,
    child_outputs: Mapping[str, Any] | None,
    rendered_child_response_text: str | None = None,
    child_result_snapshot: Mapping[str, Any] | None = None,
    selected_workflow_trace: Mapping[str, Any] | None = None,
    workflow_routing: Mapping[str, Any] | None = None,
    workflow_discovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the master-turn context payload for one selected workflow outcome.

    Keep this shaping logic shared between the interactive orchestrator path and
    the durable action registry so the conversation-turn workflow sees the same
    completion-report surface in both environments.
    """

    clean_selected_workflow_id = _safe_str(selected_workflow_id)
    child_outputs_map = (
        dict(child_outputs) if isinstance(child_outputs, Mapping) else {}
    )
    rendered_response = _safe_str(rendered_child_response_text)
    child_snapshot = (
        dict(child_result_snapshot)
        if isinstance(child_result_snapshot, Mapping)
        else None
    )
    derived_user_response = _coerce_non_empty_text(rendered_response) or (
        render_selected_workflow_user_response(
            selected_workflow_id=clean_selected_workflow_id,
            child_completed=bool(child_completed),
            final_state=_safe_str(final_state),
            failure_detail=_safe_str(failure_detail),
            child_outputs=child_outputs_map,
            child_result_snapshot=child_snapshot,
        )
    )

    completion_report = child_outputs_map.get("completion_report")
    completion_report_source = "child_completion_report"
    if not isinstance(completion_report, Mapping):
        summary_payload = child_outputs_map.get("workflow_execution_summary")
        if isinstance(summary_payload, Mapping):
            completion_report = dict(summary_payload)
            completion_report_source = "workflow_execution_summary"
        else:
            response_preview = (
                rendered_response
                or _safe_str(child_outputs_map.get("response_text"))
                or _safe_str(child_outputs_map.get("final_response"))
                or _safe_str(child_outputs_map.get("current_response"))
            )
            completion_report = {
                "schema_version": "conversation_turn_selected_workflow_result.v1",
                "workflow_id": clean_selected_workflow_id,
                "completed": bool(child_completed),
                "final_state": _safe_str(final_state),
                "error": _safe_str(failure_detail),
            }
            if response_preview:
                completion_report["response_text"] = response_preview
            completion_report_source = "synthetic_selected_workflow_summary"

    completion_report_map = dict(completion_report)
    if derived_user_response:
        completion_report_map["response_text"] = derived_user_response
    else:
        completion_report_map.pop("response_text", None)
    if child_snapshot:
        completion_report_map.setdefault("result_snapshot", dict(child_snapshot))
        for key, value in child_snapshot.items():
            if isinstance(key, str) and key not in completion_report_map:
                completion_report_map[key] = value

    final_response = derived_user_response
    current_response = derived_user_response
    response_text = derived_user_response

    outputs: dict[str, Any] = {
        "completion_report": completion_report_map,
        "selected_workflow_trace": {
            **(
                {
                    str(key): value
                    for key, value in selected_workflow_trace.items()
                    if isinstance(key, str)
                }
                if isinstance(selected_workflow_trace, Mapping)
                else {}
            ),
            "selected_workflow_id": clean_selected_workflow_id,
            "child_workflow_completed": bool(child_completed),
            "child_workflow_final_state": _safe_str(final_state),
            "child_workflow_error": _safe_str(failure_detail),
            "completion_report_source": completion_report_source,
            "child_result_snapshot": child_snapshot,
            "selected_workflow_user_response_available": bool(derived_user_response),
        },
        "workflow_routing": (
            {
                str(key): value
                for key, value in workflow_routing.items()
                if isinstance(key, str)
            }
            if isinstance(workflow_routing, Mapping)
            else None
        ),
        "workflow_discovery_result": (
            {
                str(key): value
                for key, value in workflow_discovery.items()
                if isinstance(key, str)
            }
            if isinstance(workflow_discovery, Mapping)
            else {}
        ),
        "workflow_discovery": (
            {
                str(key): value
                for key, value in workflow_discovery.items()
                if isinstance(key, str)
            }
            if isinstance(workflow_discovery, Mapping)
            else {}
        ),
        "selected_workflow_completed": bool(child_completed),
        "selected_workflow_child_failed": not bool(child_completed),
        "selected_workflow_final_state": _safe_str(final_state),
        "selected_workflow_error": _safe_str(failure_detail),
        "selected_workflow_user_response_available": bool(derived_user_response),
    }
    child_invocations = child_outputs_map.get("invocations")
    if isinstance(child_invocations, list):
        outputs["invocations"] = list(child_invocations)
    child_tool_messages = child_outputs_map.get("tool_messages")
    if isinstance(child_tool_messages, list):
        outputs["tool_messages"] = list(child_tool_messages)
    if clean_selected_workflow_id:
        outputs["selected_workflow_id"] = clean_selected_workflow_id
    if derived_user_response:
        outputs["selected_workflow_user_response"] = derived_user_response
    if final_response:
        outputs["final_response"] = final_response
    if current_response:
        outputs["current_response"] = current_response
    if response_text:
        outputs["response_text"] = response_text
    return outputs


def build_turn_recovery_tool_batch_outputs(
    *,
    requested_tool_calls: Sequence[Mapping[str, Any]],
    invocation_records: Sequence[Mapping[str, Any]],
    existing_invocations: Sequence[Mapping[str, Any]] | None = None,
    existing_tool_messages: Sequence[Mapping[str, Any]] | None = None,
    selected_workflow_trace: Mapping[str, Any] | None = None,
    selected_workflow_id: str | None = None,
    reasoning: str | None = None,
    omitted_call_count: int = 0,
) -> dict[str, Any]:
    """Build the canonical recovery-tool-batch context surface.

    Keep this payload bounded and user-facing enough for narration while making
    the direct recovery action schema visible to later diagnostics.
    """

    clean_selected_workflow_id = _safe_str(selected_workflow_id)
    bounded_tool_calls = _bounded_snapshot(list(requested_tool_calls), max_items=4)
    bounded_invocations = _bounded_snapshot(list(invocation_records), max_items=4)
    successful_count = sum(
        1
        for record in invocation_records
        if isinstance(record, Mapping)
        and _coerce_non_empty_text(record.get("status")) == "ok"
    )
    failed_records = [
        record
        for record in invocation_records
        if isinstance(record, Mapping)
        and _coerce_non_empty_text(record.get("status")) not in {None, "ok"}
    ]
    first_error = next(
        (
            _coerce_non_empty_text(record.get("error"))
            for record in failed_records
            if isinstance(record, Mapping)
            and _coerce_non_empty_text(record.get("error"))
        ),
        None,
    )
    derived_user_response = _derive_tool_batch_user_response(invocation_records)
    status = "completed"
    if failed_records:
        status = "completed_with_failures"
    if not invocation_records:
        status = "invalid"

    execution_payload: dict[str, Any] = {
        "schema_version": "conversation_turn_recovery_tool_batch_result.v1",
        "action_type": "execute_tool_batch",
        "status": status,
        "selected_workflow_id": clean_selected_workflow_id,
        "requested_tool_call_count": len(requested_tool_calls),
        "executed_tool_call_count": len(invocation_records),
        "successful_tool_call_count": successful_count,
        "failed_tool_call_count": len(failed_records),
        "tool_calls": bounded_tool_calls,
        "tool_invocations": bounded_invocations,
    }
    if omitted_call_count > 0:
        execution_payload["omitted_tool_call_count"] = max(0, int(omitted_call_count))
    if reasoning:
        execution_payload["reasoning"] = reasoning
    if first_error:
        execution_payload["error"] = first_error
    if derived_user_response:
        execution_payload["response_text"] = derived_user_response

    batch_tool_messages = [
        {"role": "tool", "content": _render_tool_batch_message_content(record)}
        for record in invocation_records
        if isinstance(record, Mapping)
    ]
    combined_invocations = [
        {
            str(key): value
            for key, value in item.items()
            if isinstance(key, str)
        }
        for item in (existing_invocations or [])
        if isinstance(item, Mapping)
    ] + [
        {
            str(key): value
            for key, value in item.items()
            if isinstance(key, str)
        }
        for item in invocation_records
        if isinstance(item, Mapping)
    ]
    combined_tool_messages = [
        {
            str(key): value
            for key, value in item.items()
            if isinstance(key, str)
        }
        for item in (existing_tool_messages or [])
        if isinstance(item, Mapping)
    ] + batch_tool_messages

    merged_selected_workflow_trace = (
        {
            str(key): value
            for key, value in selected_workflow_trace.items()
            if isinstance(key, str)
        }
        if isinstance(selected_workflow_trace, Mapping)
        else {}
    )
    merged_selected_workflow_trace["selected_execution_mode"] = "recovery_tool_batch"
    merged_selected_workflow_trace["recovery_action_type"] = "execute_tool_batch"
    merged_selected_workflow_trace["recovery_tool_batch_execution"] = dict(
        execution_payload
    )

    outputs: dict[str, Any] = {
        "completion_report": dict(execution_payload),
        "turn_recovery_tool_batch_execution": dict(execution_payload),
        "selected_workflow_trace": merged_selected_workflow_trace,
        "invocations": combined_invocations,
        "tool_messages": combined_tool_messages,
        "response_text": derived_user_response or "",
        "final_response": derived_user_response or "",
        "current_response": derived_user_response or "",
        "selected_workflow_user_response": derived_user_response or "",
        "selected_workflow_completed": len(failed_records) == 0,
        "selected_workflow_child_failed": bool(failed_records),
        "selected_workflow_final_state": (
            "recovery_tool_batch_completed"
            if not failed_records
            else "recovery_tool_batch_completed_with_failures"
        ),
        "selected_workflow_error": first_error or "",
    }
    return outputs


def run_turn_execution_critic(
    request: Any,
    *,
    annotation_component: str,
    annotation_function: str,
) -> WorkflowActionResult:
    """Build turn-execution evidence and postcondition checks."""

    data = request.data
    env = request.environment

    prompt_text = data.get("prompt")
    if not isinstance(prompt_text, str):
        prompt_text = str(prompt_text or "")

    response_text = data.get("final_response")
    if not isinstance(response_text, str):
        current_response = data.get("current_response")
        response_text = current_response if isinstance(current_response, str) else ""

    raw_tool_invocations = data.get("invocations")
    tool_invocations: Sequence[Mapping[str, Any]]
    if isinstance(raw_tool_invocations, (list, tuple)):
        tool_invocations = raw_tool_invocations
    else:
        tool_invocations = ()

    raw_aux = data.get("aux_llm_calls")
    aux_calls: Sequence[Mapping[str, Any]]
    if isinstance(raw_aux, list):
        aux_calls = raw_aux
    else:
        aux_calls = ()

    workflow_discovery = data.get("workflow_discovery_result")
    workflow_discovery_payload = (
        dict(workflow_discovery) if isinstance(workflow_discovery, Mapping) else None
    )
    workflow_routing = data.get("workflow_routing")
    workflow_routing_payload = (
        dict(workflow_routing) if isinstance(workflow_routing, Mapping) else None
    )

    turn_execution_record = build_turn_execution_record(
        request_id=data.get("turn_id"),
        session_id=data.get("conversation_session_id"),
        namespace=getattr(env, "user_namespace", None),
        actor_concept_id=data.get("actor_concept_id") or data.get("user_concept_id"),
        user_id=data.get("user_concept_id"),
        org_id=data.get("org_concept_id"),
        prompt_text=prompt_text,
        response_text=response_text,
        interaction_timestamp_utc=data.get("turn_execution_record_generated_at"),
        workflow_discovery=workflow_discovery_payload,
        workflow_routing=workflow_routing_payload,
        tool_invocations=tool_invocations,
        turn_execution_diagnostics=data.get("turn_execution_diagnostics"),
        aux_llm_calls=aux_calls,
        selected_workflow_trace=data.get("selected_workflow_trace"),
        critic_verdict=data.get("critic_verdict"),
        completion_gate_verdict=data.get("completion_gate_verdict"),
        completion_report=data.get("completion_report"),
        required_prompt_tools=(
            data.get("required_prompt_tools")
            if isinstance(data.get("required_prompt_tools"), list)
            else None
        ),
    )

    completion_gate = turn_execution_record.get("completion_gate")
    gate_requires_follow_up = False
    gate_decision = None
    gate_safe_to_claim = True
    if isinstance(completion_gate, Mapping):
        gate_requires_follow_up = bool(
            completion_gate.get("requires_follow_up", False)
        )
        raw_gate_decision = completion_gate.get("decision")
        if isinstance(raw_gate_decision, str) and raw_gate_decision.strip():
            gate_decision = raw_gate_decision.strip()
        gate_safe_to_claim = bool(
            completion_gate.get("safe_to_claim_completion", True)
        )

    critic_summary: Mapping[str, Any] = {}
    critic_payload = turn_execution_record.get("critic")
    if isinstance(critic_payload, Mapping):
        raw_summary = critic_payload.get("summary")
        if isinstance(raw_summary, Mapping):
            critic_summary = dict(raw_summary)

    required_effects = turn_execution_record.get("required_effects")
    if not isinstance(required_effects, list):
        required_effects = []
    postcondition_checks = turn_execution_record.get("postcondition_checks")
    if not isinstance(postcondition_checks, list):
        postcondition_checks = []

    if isinstance(raw_aux, list):
        try:
            raw_aux.append(
                annotate_python_decision_event(
                    {
                        "type": "turn_execution_critic",
                        "workflow_id": KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
                        "decision_preview": gate_decision,
                        "requires_follow_up": gate_requires_follow_up,
                        "required_effect_count": len(required_effects),
                        "postcondition_check_count": len(postcondition_checks),
                        "request_id": turn_execution_record.get("request_id"),
                    },
                    stage="completion_gate",
                    component=annotation_component,
                    function=annotation_function,
                    decision_class="turn_execution_critic",
                    decision_source="execution_postcondition_check",
                    changed_outcome=gate_requires_follow_up,
                    reason_code=(
                        "follow_up_required" if gate_requires_follow_up else "completed"
                    ),
                    possible_inappropriate_python_code_use=False,
                )
            )
        except Exception:
            pass

    return WorkflowActionResult(
        outputs={
            "turn_execution_record": turn_execution_record,
            "required_effects": list(required_effects),
            "postcondition_checks": list(postcondition_checks),
            "critic_summary": dict(critic_summary),
            "completion_gate_decision": gate_decision,
            "completion_gate_requires_follow_up": gate_requires_follow_up,
            "completion_gate_safe_to_claim_completion": gate_safe_to_claim,
        }
    )


def run_turn_execution_completion_gate(
    request: Any,
    *,
    annotation_component: str,
    annotation_function: str,
    introspection_auto_apply_env: str,
    loop_max_attempts_default: int = 1,
    loop_max_elapsed_ms_default: int = 60_000,
    loop_no_progress_limit_default: int = 1,
    loop_stall_max_elapsed_ms_default: int = 10_000,
) -> WorkflowActionResult:
    """Apply completion-gate policy and annotate unresolved execution."""

    data = request.data
    record = data.get("turn_execution_record")
    if not isinstance(record, Mapping):
        critic_result = run_turn_execution_critic(
            request,
            annotation_component=annotation_component,
            annotation_function=annotation_function.replace(
                "completion_gate", "critic"
            ),
        )
        rebuilt_record = critic_result.outputs.get("turn_execution_record")
        record = rebuilt_record if isinstance(rebuilt_record, Mapping) else {}

    completion_gate_payload = record.get("completion_gate")
    if not isinstance(completion_gate_payload, Mapping):
        completion_gate_payload = {}

    decision = _safe_str(completion_gate_payload.get("decision")) or "completed"
    decision_reason = _safe_str(completion_gate_payload.get("decision_reason")) or ""

    blocking_effect_ids = _normalise_string_list(
        completion_gate_payload.get("blocking_effect_ids")
    )
    blocking_failure_codes = _normalise_string_list(
        completion_gate_payload.get("blocking_failure_codes")
    )

    record_evidence_payload_raw = completion_gate_payload.get("evidence_payload")
    record_evidence_payload: dict[str, Any] = (
        dict(record_evidence_payload_raw)
        if isinstance(record_evidence_payload_raw, Mapping)
        else {}
    )
    execution_signal_blocker = record_evidence_payload.get("execution_signal_blocker")
    execution_signal_blocker = (
        dict(execution_signal_blocker)
        if isinstance(execution_signal_blocker, Mapping)
        else {}
    )

    unresolved_preconditions: list[dict[str, Any]] = []
    unresolved_preconditions_raw = record_evidence_payload.get(
        "unresolved_preconditions"
    )
    if isinstance(unresolved_preconditions_raw, list):
        for item in unresolved_preconditions_raw:
            if not isinstance(item, Mapping):
                continue
            unresolved_preconditions.append(
                {
                    "effect_id": _safe_str(item.get("effect_id")),
                    "effect_type": _safe_str(item.get("effect_type")),
                    "status": _safe_str(item.get("status")),
                    "status_reason": _safe_str(item.get("status_reason")),
                    "failure_codes": _normalise_string_list(item.get("failure_codes")),
                }
            )

    if not unresolved_preconditions:
        required_effects_raw = record.get("required_effects")
        required_effects = (
            required_effects_raw if isinstance(required_effects_raw, list) else []
        )
        for effect in required_effects:
            if not isinstance(effect, Mapping):
                continue
            effect_status = _safe_str(effect.get("status")) or ""
            if effect_status not in {"not_satisfied", "not_executed"}:
                continue
            failure_codes = _normalise_string_list(effect.get("failure_codes"))
            single_failure_code = _safe_str(effect.get("failure_code"))
            if single_failure_code and single_failure_code not in failure_codes:
                failure_codes.append(single_failure_code)
            unresolved_preconditions.append(
                {
                    "effect_id": _safe_str(effect.get("effect_id")),
                    "effect_type": _safe_str(effect.get("effect_type")),
                    "status": effect_status,
                    "status_reason": _safe_str(effect.get("status_reason")),
                    "failure_codes": failure_codes,
                }
            )

    postcondition_summary = record_evidence_payload.get("postcondition_summary")
    if not isinstance(postcondition_summary, Mapping):
        critic_payload_raw = record.get("critic")
        critic_payload = (
            critic_payload_raw if isinstance(critic_payload_raw, Mapping) else {}
        )
        summary_raw = critic_payload.get("summary")
        postcondition_summary = summary_raw if isinstance(summary_raw, Mapping) else {}

    if not blocking_failure_codes:
        for unresolved in unresolved_preconditions:
            if not isinstance(unresolved, Mapping):
                continue
            for code in _normalise_string_list(unresolved.get("failure_codes")):
                if code not in blocking_failure_codes:
                    blocking_failure_codes.append(code)
    if not blocking_failure_codes and unresolved_preconditions:
        unresolved_statuses = {
            str(unresolved.get("status") or "").strip()
            for unresolved in unresolved_preconditions
            if isinstance(unresolved, Mapping)
        }
        blocking_failure_codes.append("required_effects_unresolved")
        if "not_satisfied" in unresolved_statuses:
            blocking_failure_codes.append("required_effect_not_satisfied")
        if "not_executed" in unresolved_statuses:
            blocking_failure_codes.append("required_effect_not_executed")
    if not blocking_failure_codes:
        if decision == "failed":
            blocking_failure_codes.append("required_effect_not_satisfied")
        elif decision == "escalation_required":
            blocking_failure_codes.append("required_effect_not_executed")
        elif decision == "partial":
            blocking_failure_codes.append("postcondition_inconclusive")
    blocking_failure_codes = sorted(set(blocking_failure_codes))

    safe_to_claim_completion = bool(
        completion_gate_payload.get("safe_to_claim_completion", True)
    )
    requires_follow_up = bool(
        completion_gate_payload.get("requires_follow_up", False)
    )
    repeat_eligible_default = False if execution_signal_blocker else requires_follow_up
    repeat_eligible = bool(
        completion_gate_payload.get("repeat_eligible", repeat_eligible_default)
    )
    loop_attempts = _coerce_non_negative_int(
        data.get("completion_gate_loop_attempts"),
        default=0,
        max_value=20,
    )
    loop_max_attempts = _coerce_non_negative_int(
        data.get("completion_gate_loop_max_attempts"),
        default=loop_max_attempts_default,
        max_value=20,
    )
    loop_max_elapsed_ms = _coerce_non_negative_int(
        data.get("completion_gate_loop_max_elapsed_ms"),
        default=loop_max_elapsed_ms_default,
        max_value=600_000,
    )
    loop_no_progress_streak = _coerce_non_negative_int(
        data.get("completion_gate_loop_no_progress_streak"),
        default=0,
        max_value=20,
    )
    loop_no_progress_limit = _coerce_non_negative_int(
        data.get("completion_gate_loop_no_progress_limit"),
        default=loop_no_progress_limit_default,
        max_value=20,
    )
    loop_last_invocation_count = _coerce_non_negative_int(
        data.get("completion_gate_loop_last_invocation_count"),
        default=0,
        max_value=100_000,
    )
    loop_last_blocking_signature = _safe_str(
        data.get("completion_gate_loop_last_blocking_signature")
    ) or ""
    current_monotonic = time.monotonic()
    loop_started_monotonic_raw = data.get("completion_gate_loop_started_monotonic")
    if isinstance(loop_started_monotonic_raw, (int, float)):
        loop_started_monotonic = float(loop_started_monotonic_raw)
    else:
        loop_started_monotonic = current_monotonic
    loop_elapsed_ms = max(0, int((current_monotonic - loop_started_monotonic) * 1000))
    loop_stall_started_monotonic_raw = data.get(
        "completion_gate_loop_stall_started_monotonic"
    )
    loop_stall_started_monotonic = (
        float(loop_stall_started_monotonic_raw)
        if isinstance(loop_stall_started_monotonic_raw, (int, float))
        else None
    )
    loop_stall_events = _coerce_non_negative_int(
        data.get("completion_gate_loop_stall_events"),
        default=0,
        max_value=100,
    )
    loop_stall_max_elapsed_ms = _coerce_non_negative_int(
        data.get("completion_gate_loop_stall_max_elapsed_ms"),
        default=loop_stall_max_elapsed_ms_default,
        max_value=600_000,
    )
    loop_stall_elapsed_ms = _coerce_non_negative_int(
        data.get("completion_gate_loop_stall_elapsed_ms"),
        default=0,
        max_value=600_000,
    )

    final_response = data.get("final_response")
    if not isinstance(final_response, str):
        current_response = data.get("current_response")
        final_response = current_response if isinstance(current_response, str) else ""

    if not safe_to_claim_completion:
        unresolved_effect_types = {
            str(unresolved.get("effect_type") or "").strip()
            for unresolved in unresolved_preconditions
            if isinstance(unresolved, Mapping)
            and isinstance(unresolved.get("effect_type"), str)
            and str(unresolved.get("effect_type") or "").strip()
        }
        if unresolved_effect_types == {"tool_execution"}:
            if decision == "failed":
                status_line = (
                    "Execution status: planned tool execution did not complete successfully."
                )
            else:
                status_line = (
                    "Execution status: required tool execution was not completed."
                )
        elif unresolved_effect_types == {"workflow_execution"}:
            status_line = (
                "Execution status: selected workflow execution did not complete successfully."
            )
        elif decision == "failed":
            status_line = "Execution status: requested mutation failed or was blocked."
        elif decision == "escalation_required":
            status_line = "Execution status: requested mutation was not executed."
        elif decision == "partial":
            status_line = (
                "Execution status: mutation may have run but verification is inconclusive."
            )
        else:
            status_line = "Execution status: follow-up verification is required."

        if decision_reason:
            status_line = f"{status_line} {decision_reason}"
        if blocking_effect_ids:
            status_line = (
                f"{status_line} Blocking effect IDs: {', '.join(blocking_effect_ids)}."
            )
        if unresolved_preconditions:
            unresolved_reasons: list[str] = []
            unresolved_failure_codes: list[str] = []
            for unresolved in unresolved_preconditions:
                if not isinstance(unresolved, Mapping):
                    continue
                reason = unresolved.get("status_reason")
                if isinstance(reason, str) and reason.strip():
                    unresolved_reasons.append(reason.strip())
                for code in _normalise_string_list(unresolved.get("failure_codes")):
                    if code not in unresolved_failure_codes:
                        unresolved_failure_codes.append(code)
            if unresolved_reasons:
                status_line = (
                    f"{status_line} Unresolved preconditions: "
                    f"{'; '.join(unresolved_reasons[:2])}."
                )
            if unresolved_failure_codes:
                status_line = (
                    f"{status_line} Failure codes: "
                    f"{', '.join(unresolved_failure_codes[:3])}."
                )

        ledger_replaced_empty = not (
            isinstance(final_response, str) and final_response.strip()
        )
        if isinstance(final_response, str) and final_response.strip():
            if "Execution status:" not in final_response:
                final_response = f"{final_response.rstrip()}\n\n{status_line}"
        else:
            final_response = status_line

        aux_llm_calls = data.get("aux_llm_calls")
        if isinstance(aux_llm_calls, list):
            try:
                aux_llm_calls.append(
                    annotate_python_decision_event(
                        {
                            "type": "completion_ledger_injection",
                            "decision": decision,
                            "ledger_replaced_empty_response": ledger_replaced_empty,
                            "decision_authority": {
                                "origin": "python",
                                "decision_class": "completion_ledger_injection",
                                "scaffolding": True,
                            },
                        },
                        stage="completion_gate",
                        component=annotation_component,
                        function=annotation_function,
                        decision_class="completion_ledger_injection",
                        decision_source="execution_postcondition_check",
                        changed_outcome=True,
                        reason_code=decision or "unknown",
                        possible_inappropriate_python_code_use=True,
                    )
                )
            except Exception:
                pass

    invocations_raw = data.get("invocations")
    invocation_count = len(invocations_raw) if isinstance(invocations_raw, list) else 0
    blocking_signature = f"{decision}:{'|'.join(sorted(blocking_effect_ids))}"
    progress_observed = invocation_count > loop_last_invocation_count
    same_blocking_signature = (
        bool(loop_last_blocking_signature)
        and loop_last_blocking_signature == blocking_signature
    )
    stalled_checkpoint = (
        requires_follow_up and (not progress_observed) and same_blocking_signature
    )
    if stalled_checkpoint:
        loop_no_progress_streak += 1
        if loop_stall_started_monotonic is None:
            loop_stall_started_monotonic = current_monotonic
        loop_stall_events += 1
        loop_stall_elapsed_ms = max(
            0,
            int((current_monotonic - loop_stall_started_monotonic) * 1000),
        )
    else:
        loop_no_progress_streak = 0
        loop_stall_started_monotonic = None
        loop_stall_elapsed_ms = 0

    repeat_iteration = False
    repeat_stop_reason: str | None = None
    loop_retry_reason: str | None = None
    terminal_non_repeatable = False

    if requires_follow_up:
        if not repeat_eligible:
            terminal_non_repeatable = True
            repeat_stop_reason = "terminal_execution_failure"
        elif loop_attempts >= loop_max_attempts:
            repeat_stop_reason = "attempt_budget_exhausted"
        elif loop_elapsed_ms >= loop_max_elapsed_ms:
            repeat_stop_reason = "elapsed_budget_exhausted"
        elif (
            loop_stall_max_elapsed_ms > 0
            and loop_stall_elapsed_ms >= loop_stall_max_elapsed_ms
        ):
            repeat_stop_reason = "stall_latency_budget_exhausted"
        elif (
            loop_no_progress_limit > 0
            and loop_attempts > 0
            and loop_no_progress_streak >= loop_no_progress_limit
        ):
            repeat_stop_reason = "no_progress_guard_triggered"
        else:
            repeat_iteration = True
            loop_attempts += 1
            reason_parts: list[str] = []
            if decision_reason:
                reason_parts.append(decision_reason)
            if blocking_effect_ids:
                reason_parts.append(
                    "Blocking effect IDs: " + ", ".join(blocking_effect_ids)
                )
            loop_retry_reason = (
                " ".join(reason_parts) if reason_parts else "Required effects unresolved."
            )

    if repeat_iteration:
        safe_to_claim_completion = False
        requires_follow_up = True

    terminal_outcome = "completed"
    if repeat_iteration:
        terminal_outcome = "retrying"
    elif terminal_non_repeatable:
        terminal_outcome = "failed" if decision == "failed" else "follow_up_required"
    elif requires_follow_up and repeat_stop_reason:
        terminal_outcome = repeat_stop_reason
    elif requires_follow_up:
        terminal_outcome = "follow_up_required"
    escalation_signal = bool(
        requires_follow_up
        and not repeat_iteration
        and (terminal_non_repeatable or repeat_stop_reason)
    )
    escalation_reason = repeat_stop_reason if escalation_signal else None

    completion_gate_evidence_payload: dict[str, Any] = dict(record_evidence_payload)
    completion_gate_evidence_payload.update(
        {
            "decision": decision,
            "decision_reason": decision_reason,
            "safe_to_claim_completion": safe_to_claim_completion,
            "requires_follow_up": requires_follow_up,
            "repeat_eligible": repeat_eligible,
            "blocking_effect_ids": list(blocking_effect_ids),
            "blocking_failure_codes": list(blocking_failure_codes),
            "unresolved_preconditions": unresolved_preconditions,
            "postcondition_summary": (
                dict(postcondition_summary)
                if isinstance(postcondition_summary, Mapping)
                else {}
            ),
            "terminal_outcome": terminal_outcome,
            "repeat_iteration": repeat_iteration,
            "repeat_stop_reason": repeat_stop_reason,
            "loop_retry_reason": loop_retry_reason,
            "loop_stall_events": loop_stall_events,
            "loop_stall_elapsed_ms": loop_stall_elapsed_ms,
            "loop_stall_max_elapsed_ms": loop_stall_max_elapsed_ms,
            "escalation_signal": escalation_signal,
            "escalation_reason": escalation_reason,
        }
    )

    introspection_autotrigger: dict[str, Any] | None = None
    from ...services.workflow_event_integration_service import (
        episode_evaluation_autotrigger_enabled,
        maybe_launch_episode_evaluation_for_turn_completion_gate,
    )

    autotrigger_enabled = episode_evaluation_autotrigger_enabled()
    already_autotriggered = bool(
        data.get("workflow_introspection_autotriggered")
        or data.get("episode_evaluation_autotriggered")
    )
    if (
        autotrigger_enabled
        and requires_follow_up
        and not repeat_iteration
        and not already_autotriggered
    ):
        namespace = _safe_str(getattr(request.environment, "user_namespace", None)) or _safe_str(
            data.get("namespace")
        )
        user_concept_id = _safe_str(data.get("user_concept_id")) or _safe_str(
            data.get("actor_concept_id")
        )
        org_concept_id = _safe_str(data.get("org_concept_id"))
        selected_workflow_id = None
        workflow_selection = (
            record.get("workflow_selection")
            if isinstance(record.get("workflow_selection"), Mapping)
            else {}
        )
        if isinstance(workflow_selection, Mapping):
            selected_workflow_id = _safe_str(
                workflow_selection.get("selected_workflow_id")
            )

        auto_apply_repairs = os.getenv(
            introspection_auto_apply_env,
            "1",
        ).strip().lower() in {"1", "true", "yes", "on"}
        request_id = _safe_str(data.get("turn_id"))
        conversation_session_id = _safe_str(data.get("conversation_session_id"))
        prompt_text = str(data.get("prompt") or "").strip()
        incident_text = prompt_text[:500] if prompt_text else str(decision_reason or "")[:500]
        try:
            introspection_autotrigger = (
                maybe_launch_episode_evaluation_for_turn_completion_gate(
                    request_id=request_id,
                    session_id=conversation_session_id,
                    namespace=namespace,
                    user_id=user_concept_id or "anonymous",
                    org_id=org_concept_id or "default",
                    selected_workflow_id=selected_workflow_id,
                    incident_text=incident_text,
                    maintenance_apply_repairs_default=auto_apply_repairs,
                    current_depth=int(data.get("episode_evaluation_depth") or 0),
                )
            )
            ok = bool(introspection_autotrigger.get("success")) and bool(
                introspection_autotrigger.get("triggered")
                or introspection_autotrigger.get("idempotent_reused")
            )
            data["workflow_introspection_autotriggered"] = ok
            data["episode_evaluation_autotriggered"] = ok
        except Exception as exc:
            introspection_autotrigger = {
                "attempted": True,
                "enabled": True,
                "success": False,
                "workflow_id": "#V#episode_evaluation_workflow",
                "error": str(exc),
                "error_code": "episode_evaluation_autotrigger_failed",
            }
            data["workflow_introspection_autotriggered"] = False
            data["episode_evaluation_autotriggered"] = False
    elif autotrigger_enabled:
        introspection_autotrigger = {
            "attempted": False,
            "enabled": True,
            "reason": (
                "repeat_iteration"
                if repeat_iteration
                else "already_autotriggered"
                if already_autotriggered
                else "no_follow_up_required"
            ),
            "workflow_id": "#V#episode_evaluation_workflow",
        }
    else:
        introspection_autotrigger = {
            "attempted": False,
            "enabled": False,
            "reason": "autotrigger_disabled",
            "workflow_id": "#V#episode_evaluation_workflow",
        }

    aux_llm_calls = data.get("aux_llm_calls")
    autotrigger_attempted = bool(
        introspection_autotrigger and introspection_autotrigger.get("attempted")
    )
    autotrigger_reason = (
        str(introspection_autotrigger.get("reason") or "")
        if introspection_autotrigger
        else ""
    )
    if isinstance(aux_llm_calls, list):
        try:
            aux_llm_calls.append(
                annotate_python_decision_event(
                    {
                        "type": "episode_evaluation_autotrigger_gate",
                        "workflow_id": "#V#episode_evaluation_workflow",
                        "enabled": autotrigger_enabled,
                        "attempted": autotrigger_attempted,
                        "triggered": bool(
                            introspection_autotrigger
                            and (
                                introspection_autotrigger.get("triggered")
                                or introspection_autotrigger.get("idempotent_reused")
                            )
                        ),
                        "gate_inputs": {
                            "requires_follow_up": requires_follow_up,
                            "repeat_iteration": repeat_iteration,
                            "already_autotriggered": already_autotriggered,
                        },
                        "skip_reason": autotrigger_reason or None,
                    },
                    stage="completion_gate",
                    component=annotation_component,
                    function=annotation_function,
                    decision_class="episode_evaluation_autotrigger_gate",
                    decision_source="gate_check",
                    changed_outcome=autotrigger_attempted,
                    reason_code=(
                        "autotrigger_launched"
                        if autotrigger_attempted
                        else autotrigger_reason or "autotrigger_skipped"
                    ),
                    possible_inappropriate_python_code_use=False,
                )
            )
        except Exception:
            pass

    if isinstance(aux_llm_calls, list):
        try:
            if execution_signal_blocker:
                blocker_failure_codes = _normalise_string_list(
                    execution_signal_blocker.get("failure_codes")
                )
                blocker_failure_code = _safe_str(
                    execution_signal_blocker.get("failure_code")
                )
                if (
                    blocker_failure_code
                    and blocker_failure_code not in blocker_failure_codes
                ):
                    blocker_failure_codes.append(blocker_failure_code)
                aux_llm_calls.append(
                    annotate_python_decision_event(
                        {
                            "type": "execution_signal_blocker_override",
                            "workflow_id": TURN_COMPLETION_GATE_WORKFLOW_ID,
                            "overridden_decision": "completed",
                            "decision": decision,
                            "decision_reason": decision_reason,
                            "repeat_eligible": repeat_eligible,
                            "effect_id": execution_signal_blocker.get("effect_id"),
                            "effect_type": execution_signal_blocker.get("effect_type"),
                            "status": execution_signal_blocker.get("status"),
                            "status_reason": execution_signal_blocker.get(
                                "status_reason"
                            ),
                            "failure_codes": blocker_failure_codes,
                            "source": execution_signal_blocker.get("source"),
                            "workflow_id_blocked": execution_signal_blocker.get(
                                "workflow_id"
                            ),
                        },
                        stage="completion_gate",
                        component=annotation_component,
                        function=annotation_function,
                        decision_class="execution_signal_blocker_override",
                        decision_source="execution_signals",
                        changed_outcome=True,
                        reason_code=(
                            blocker_failure_codes[0]
                            if blocker_failure_codes
                            else decision or "unknown"
                        ),
                        possible_inappropriate_python_code_use=False,
                    )
                )
            aux_llm_calls.append(
                annotate_python_decision_event(
                    {
                        "type": "turn_completion_gate",
                        "workflow_id": TURN_COMPLETION_GATE_WORKFLOW_ID,
                        "decision": decision,
                        "decision_reason": decision_reason,
                        "safe_to_claim_completion": safe_to_claim_completion,
                        "requires_follow_up": requires_follow_up,
                        "repeat_eligible": repeat_eligible,
                        "blocking_effect_ids": list(blocking_effect_ids),
                        "blocking_failure_codes": list(blocking_failure_codes),
                        "execution_signal_blocker_triggered": bool(
                            execution_signal_blocker
                        ),
                        "unresolved_preconditions": unresolved_preconditions,
                        "terminal_outcome": terminal_outcome,
                        "evidence_payload": completion_gate_evidence_payload,
                        "repeat_iteration": repeat_iteration,
                        "repeat_stop_reason": repeat_stop_reason,
                        "loop_attempts": loop_attempts,
                        "loop_max_attempts": loop_max_attempts,
                        "loop_elapsed_ms": loop_elapsed_ms,
                        "loop_max_elapsed_ms": loop_max_elapsed_ms,
                        "loop_no_progress_streak": loop_no_progress_streak,
                        "loop_no_progress_limit": loop_no_progress_limit,
                        "loop_stall_events": loop_stall_events,
                        "loop_stall_elapsed_ms": loop_stall_elapsed_ms,
                        "loop_stall_max_elapsed_ms": loop_stall_max_elapsed_ms,
                        "escalation_signal": escalation_signal,
                        "escalation_reason": escalation_reason,
                        "loop_retry_reason": loop_retry_reason,
                        "workflow_introspection_autotrigger": introspection_autotrigger,
                        "episode_evaluation_autotrigger": introspection_autotrigger,
                    },
                    stage="completion_gate",
                    component=annotation_component,
                    function=annotation_function,
                    decision_class="turn_completion_gate",
                    decision_source="execution_postcondition_check",
                    changed_outcome=bool(requires_follow_up or repeat_iteration),
                    reason_code=terminal_outcome,
                    possible_inappropriate_python_code_use=False,
                )
            )
        except Exception:
            pass

    return WorkflowActionResult(
        outputs={
            "final_response": final_response,
            "completion_gate_decision": decision,
            "completion_gate_decision_reason": decision_reason,
            "completion_gate_blocking_effect_ids": list(blocking_effect_ids),
            "completion_gate_blocking_failure_codes": list(blocking_failure_codes),
            "completion_gate_safe_to_claim_completion": safe_to_claim_completion,
            "completion_gate_requires_follow_up": requires_follow_up,
            "completion_gate_repeat_eligible": repeat_eligible,
            "completion_gate_unresolved_preconditions": unresolved_preconditions,
            "completion_gate_evidence_payload": completion_gate_evidence_payload,
            "completion_gate_terminal_outcome": terminal_outcome,
            "completion_gate_repeat_iteration": repeat_iteration,
            "completion_gate_loop_retry_reason": loop_retry_reason,
            "completion_gate_loop_stop_reason": repeat_stop_reason,
            "completion_gate_loop_attempts": loop_attempts,
            "completion_gate_loop_max_attempts": loop_max_attempts,
            "completion_gate_loop_elapsed_ms": loop_elapsed_ms,
            "completion_gate_loop_max_elapsed_ms": loop_max_elapsed_ms,
            "completion_gate_loop_started_monotonic": loop_started_monotonic,
            "completion_gate_loop_no_progress_streak": loop_no_progress_streak,
            "completion_gate_loop_no_progress_limit": loop_no_progress_limit,
            "completion_gate_loop_stall_events": loop_stall_events,
            "completion_gate_loop_stall_elapsed_ms": loop_stall_elapsed_ms,
            "completion_gate_loop_stall_max_elapsed_ms": loop_stall_max_elapsed_ms,
            "completion_gate_loop_stall_started_monotonic": (
                loop_stall_started_monotonic
            ),
            "completion_gate_loop_last_invocation_count": invocation_count,
            "completion_gate_loop_last_blocking_signature": blocking_signature,
            "completion_gate_escalation_signal": escalation_signal,
            "completion_gate_escalation_reason": escalation_reason,
            "workflow_introspection_autotrigger": introspection_autotrigger,
            "episode_evaluation_autotrigger": introspection_autotrigger,
            "result": requires_follow_up,
        }
    )
