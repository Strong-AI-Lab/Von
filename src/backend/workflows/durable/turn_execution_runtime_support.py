"""Shared turn-execution support for orchestrator and durable runtimes.

Keep the supervising turn workflow's critic/completion-gate logic available to
both the interactive orchestrator and the durable action registry so runnable
verification and runtime behaviour stay in sync.
"""

from __future__ import annotations

import json
import os
import re
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
from ..execution_contracts import WORKFLOW_STEP_RESULT_ENVELOPES_KEY
from ..turn_expected_outcome_contract import (
    TurnExpectedOutcomeContract,
    build_turn_expected_outcome_boundary_payload,
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


def _coerce_optional_non_negative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        coerced = int(value)
    except Exception:
        return None
    return coerced if coerced >= 0 else None


def _coerce_optional_positive_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        coerced = float(value)
    except Exception:
        return None
    return coerced if coerced > 0 else None


def _merge_workflow_retry_budget_evidence(
    target: dict[str, Any],
    source: Mapping[str, Any] | None,
) -> None:
    if not isinstance(source, Mapping):
        return
    for key in ("instance_id", "workflow_id", "status", "retry_count", "max_retries"):
        if target.get(key) not in (None, ""):
            continue
        value = source.get(key)
        if value not in (None, ""):
            target[key] = value


def _extract_workflow_retry_budget_evidence(
    payload: Mapping[str, Any] | None,
    *,
    depth: int = 0,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or depth > 3:
        return {}
    evidence: dict[str, Any] = {}
    workflow_instance = payload.get("workflow_instance")
    if isinstance(workflow_instance, Mapping):
        _merge_workflow_retry_budget_evidence(evidence, workflow_instance)
    _merge_workflow_retry_budget_evidence(evidence, payload)
    for nested_key in (
        "selected_workflow_trace",
        "child_result_snapshot",
        "result_snapshot",
        "workflow_execution_summary",
        "workflow_execution",
        "completion_report",
        "completion_gate_evidence_payload",
    ):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            _merge_workflow_retry_budget_evidence(
                evidence,
                _extract_workflow_retry_budget_evidence(nested, depth=depth + 1),
            )
    return evidence


def _build_turn_execution_budget_diagnostics(
    *,
    data: Mapping[str, Any],
    environment: Any,
    invocation_count: int,
    completion_gate_loop_attempts: int,
    completion_gate_loop_max_attempts: int,
    completion_gate_loop_elapsed_ms: int,
    completion_gate_loop_max_elapsed_ms: int,
    completion_gate_loop_no_progress_streak: int,
    completion_gate_loop_no_progress_limit: int,
    completion_gate_loop_stall_elapsed_ms: int,
    completion_gate_loop_stall_max_elapsed_ms: int,
    completion_gate_repeat_stop_reason: str | None,
) -> dict[str, Any]:
    model_budget_policy = data.get("model_execution_budget_policy")
    model_timeout_sec = _coerce_optional_positive_float(
        data.get("conversation_turn_llm_timeout_override_sec")
    )
    model_timeout_source = "conversation_turn_llm_timeout_override_sec"
    if model_timeout_sec is None and isinstance(model_budget_policy, Mapping):
        model_timeout_sec = _coerce_optional_positive_float(
            model_budget_policy.get("conversation_turn_llm_timeout_sec")
        )
        model_timeout_source = "model_execution_budget_policy"

    missing_tool_retry_attempts = _coerce_non_negative_int(
        data.get("missing_tool_call_retry_attempts"),
        default=0,
        max_value=20,
    )
    missing_tool_retry_budget = _coerce_non_negative_int(
        data.get("missing_tool_call_retry_budget"),
        default=0,
        max_value=20,
    )
    max_tool_invocations = _coerce_optional_non_negative_int(
        getattr(environment, "max_tool_invocations", None)
    )
    if max_tool_invocations is None:
        max_tool_invocations = _coerce_optional_non_negative_int(
            data.get("max_tool_invocations")
        )
    retry_evidence = _extract_workflow_retry_budget_evidence(data)
    retry_count = _coerce_optional_non_negative_int(retry_evidence.get("retry_count"))
    max_retries = _coerce_optional_non_negative_int(retry_evidence.get("max_retries"))

    return {
        "schema_version": "turn_execution_budget_diagnostics.v1",
        "model_llm_timeout": {
            "timeout_sec": model_timeout_sec,
            "source": model_timeout_source if model_timeout_sec is not None else None,
            "configured": model_timeout_sec is not None,
        },
        "completion_gate_loop": {
            "attempts": completion_gate_loop_attempts,
            "max_attempts": completion_gate_loop_max_attempts,
            "elapsed_ms": completion_gate_loop_elapsed_ms,
            "max_elapsed_ms": completion_gate_loop_max_elapsed_ms,
            "no_progress_streak": completion_gate_loop_no_progress_streak,
            "no_progress_limit": completion_gate_loop_no_progress_limit,
            "stall_elapsed_ms": completion_gate_loop_stall_elapsed_ms,
            "stall_max_elapsed_ms": completion_gate_loop_stall_max_elapsed_ms,
            "stop_reason": completion_gate_repeat_stop_reason,
        },
        "durable_workflow_retry": {
            "instance_id": _safe_str(retry_evidence.get("instance_id")),
            "workflow_id": _safe_str(retry_evidence.get("workflow_id")),
            "status": _safe_str(retry_evidence.get("status")),
            "retry_count": retry_count,
            "max_retries": max_retries,
            "retry_budget_exhausted": (
                retry_count is not None
                and max_retries is not None
                and retry_count >= max_retries
            ),
        },
        "missing_tool_call_retry": {
            "attempts": missing_tool_retry_attempts,
            "budget": missing_tool_retry_budget,
            "remaining": max(
                0, missing_tool_retry_budget - missing_tool_retry_attempts
            ),
            "suppressed": bool(data.get("missing_tool_call_retry_suppressed")),
            "stop_reason": _safe_str(data.get("missing_tool_call_retry_stop_reason")),
            "recovery_outcome": _safe_str(
                data.get("missing_tool_call_recovery_outcome")
            ),
        },
        "internal_mcp_tool_invocations": {
            "observed_invocation_count": max(0, int(invocation_count)),
            "max_tool_invocations": max_tool_invocations,
            "remaining": (
                max(0, max_tool_invocations - max(0, int(invocation_count)))
                if max_tool_invocations is not None
                else None
            ),
            "settings_key": "internal_mcp_max_tool_invocations",
            "budget_exhausted": (
                max_tool_invocations is not None
                and max(0, int(invocation_count)) >= max_tool_invocations
            ),
        },
    }


def _is_evidence_effect_type(effect_type: str | None) -> bool:
    if not isinstance(effect_type, str):
        return False
    lowered = effect_type.strip().lower()
    return (
        lowered == "required_evidence"
        or lowered == "grounded_evidence"
        or lowered == "grounded_retrieval"
        or lowered.endswith("_retrieval")
        or lowered.endswith("_evidence")
        or ("evidence" in lowered)
    )


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _normalise_string_list(raw_values: Any) -> list[str]:
    normalised: list[str] = []
    if not isinstance(raw_values, list):
        return normalised
    for item in raw_values:
        cleaned = _safe_str(item)
        if cleaned:
            normalised.append(cleaned)
    return normalised


def _dedupe_string_sequence(raw_values: Any) -> list[str]:
    if not isinstance(raw_values, Sequence) or isinstance(
        raw_values, (str, bytes, bytearray)
    ):
        return []

    normalised: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        cleaned = _safe_str(item)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised.append(cleaned)
    return normalised


def _filter_string_sequence_to_allowed(
    raw_values: Any,
    allowed_values: Any,
) -> list[str]:
    values = _dedupe_string_sequence(raw_values)
    allowed = _dedupe_string_sequence(allowed_values)
    if not allowed:
        return values
    allowed_lower = {item.lower() for item in allowed}
    return [item for item in values if item.lower() in allowed_lower]


def _is_mapping_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _copy_mapping_sequence(value: Any) -> list[dict[str, Any]]:
    if not _is_mapping_sequence(value):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _merge_mapping_sequences(
    *values: Any,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for value in values:
        for item in _copy_mapping_sequence(value):
            if item in merged:
                continue
            merged.append(item)
    return merged


def _entry_workflow_id(entry: Mapping[str, Any]) -> str | None:
    return (
        _safe_str(entry.get("workflow_id"))
        or _safe_str(entry.get("selected_workflow_id"))
        or _safe_str(entry.get("dispatch_workflow_id"))
    )


def _entry_matches_selected_workflow(
    entry: Mapping[str, Any],
    *,
    selected_workflow_id: str | None,
) -> bool:
    selected = _safe_str(selected_workflow_id)
    if not selected:
        return False
    selected_lower = selected.lower()
    for key in ("workflow_id", "selected_workflow_id", "dispatch_workflow_id"):
        candidate = _safe_str(entry.get(key))
        if candidate and candidate.lower() == selected_lower:
            return True
    return False


def _trace_identifier_fields(entry: Mapping[str, Any]) -> dict[str, str]:
    execution_trace_id = _safe_str(entry.get("execution_trace_id"))
    execution_id = _safe_str(entry.get("execution_id")) or execution_trace_id
    instance_id = (
        _safe_str(entry.get("instance_id"))
        or _safe_str(entry.get("workflow_instance_id"))
        or _safe_str(entry.get("workflow_instance_concept_id"))
    )
    fields: dict[str, str] = {}
    if execution_id:
        fields["execution_id"] = execution_id
    if execution_trace_id:
        fields["execution_trace_id"] = execution_trace_id
    if instance_id:
        fields["instance_id"] = instance_id
        fields["workflow_instance_id"] = instance_id
    return fields


def _selected_child_trace_failure_code(entry: Mapping[str, Any]) -> str | None:
    for key in (
        "reason_code",
        "dispatch_failure_code",
        "error_code",
        "status",
        "final_state",
    ):
        value = _safe_str(entry.get(key))
        if value:
            return value.split(":", 1)[0].strip() or value

    termination_reason = entry.get("termination_reason")
    if isinstance(termination_reason, Mapping):
        code = _safe_str(termination_reason.get("code"))
        if code:
            return code
    return None


def _selected_child_trace_failure_detail(entry: Mapping[str, Any]) -> str | None:
    for key in (
        "dispatch_terminal_failure_detail",
        "error",
        "message",
        "detail",
        "final_state",
    ):
        value = _safe_str(entry.get(key))
        if value:
            return value

    termination_reason = entry.get("termination_reason")
    if isinstance(termination_reason, Mapping):
        detail = _safe_str(termination_reason.get("detail"))
        if detail:
            return detail
    return None


def _build_selected_child_trace_bridge_from_entry(
    entry: Mapping[str, Any],
    *,
    selected_workflow_id: str | None,
    source: str,
) -> dict[str, Any]:
    workflow_id = _entry_workflow_id(entry) or _safe_str(selected_workflow_id)
    bridge: dict[str, Any] = {
        "trace_role": "selected_workflow",
        "selected_child_trace_source": source,
    }
    if workflow_id:
        bridge["workflow_id"] = workflow_id
    bridge.update(_trace_identifier_fields(entry))

    for key in (
        "episode_id",
        "workflow_instance_created_new",
        "source",
        "session_id",
        "turn_id",
        "completed",
        "terminal_stage",
        "final_state",
        "workflow_definition_identity",
    ):
        value = entry.get(key)
        if value is not None:
            bridge[key] = value

    termination_reason = entry.get("termination_reason")
    if isinstance(termination_reason, Mapping):
        bridge["termination_reason"] = {
            str(key): value
            for key, value in termination_reason.items()
            if isinstance(key, str)
        }

    if bridge.get("execution_id") or bridge.get("instance_id"):
        return bridge

    failure_code = _selected_child_trace_failure_code(entry)
    failure_detail = _selected_child_trace_failure_detail(entry)
    bridge["trace_unavailable"] = True
    bridge["trace_unavailable_reason"] = (
        failure_code or "selected_workflow_trace_ref_missing"
    )
    bridge["selected_workflow_pre_trace_failure"] = {
        "source": source,
        "status": _safe_str(entry.get("status")),
        "failure_code": failure_code,
        "failure_detail": failure_detail,
        "workflow_id": workflow_id,
    }
    if failure_code:
        bridge["dispatch_failure_code"] = failure_code
    if failure_detail:
        bridge["dispatch_failure_detail"] = failure_detail
    return bridge


def _selected_child_trace_bridge_from_aux_entries(
    *,
    selected_workflow_id: str | None,
    aux_entries: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    if not _is_mapping_sequence(aux_entries):
        return None
    matching_without_trace: dict[str, Any] | None = None
    relevant_types = {
        "workflow_execution_trace",
        "workflow_use_episode",
        "workflow_instance_submission",
        "workflow_dispatch_boundary",
    }
    for entry in reversed(_copy_mapping_sequence(aux_entries)):
        entry_type = _safe_str(entry.get("type"))
        if entry_type not in relevant_types:
            continue
        if not _entry_matches_selected_workflow(
            entry,
            selected_workflow_id=selected_workflow_id,
        ):
            continue
        bridge = _build_selected_child_trace_bridge_from_entry(
            entry,
            selected_workflow_id=selected_workflow_id,
            source=entry_type or "aux_llm_calls",
        )
        if bridge.get("execution_id") or bridge.get("instance_id"):
            return bridge
        if matching_without_trace is None:
            matching_without_trace = bridge
    return matching_without_trace


def _merge_selected_child_trace_bridge(
    *,
    selected_workflow_trace_payload: dict[str, Any],
    selected_workflow_trace_map: dict[str, Any],
    completion_report_map: dict[str, Any],
    selected_workflow_id: str | None,
    child_completed: bool,
    final_state: str | None,
    failure_detail: str | None,
    aux_entries: Sequence[Mapping[str, Any]] | None,
) -> None:
    existing_ids = _trace_identifier_fields(selected_workflow_trace_map)
    bridge = _selected_child_trace_bridge_from_aux_entries(
        selected_workflow_id=selected_workflow_id,
        aux_entries=aux_entries,
    )

    if (
        bridge is None
        and selected_workflow_id
        and not child_completed
        and not existing_ids
    ):
        reason = (
            _safe_str(failure_detail)
            or _safe_str(final_state)
            or "selected_workflow_failed_without_durable_trace"
        )
        bridge = {
            "trace_role": "selected_workflow",
            "workflow_id": selected_workflow_id,
            "trace_unavailable": True,
            "trace_unavailable_reason": reason.split(":", 1)[0].strip() or reason,
            "selected_child_trace_source": "selected_workflow_trace",
            "selected_workflow_pre_trace_failure": {
                "source": "selected_workflow_trace",
                "status": "failed",
                "failure_code": reason.split(":", 1)[0].strip() or reason,
                "failure_detail": reason,
                "workflow_id": selected_workflow_id,
            },
        }

    if not isinstance(bridge, Mapping):
        return
    if existing_ids and not (bridge.get("execution_id") or bridge.get("instance_id")):
        return

    for key, value in bridge.items():
        if value is None:
            continue
        if key in {"execution_id", "instance_id", "workflow_instance_id"}:
            if not selected_workflow_trace_map.get(key):
                selected_workflow_trace_map[key] = value
            if not selected_workflow_trace_payload.get(key):
                selected_workflow_trace_payload[key] = value
            if not completion_report_map.get(key):
                completion_report_map[key] = value
            continue
        selected_workflow_trace_map.setdefault(key, value)
        selected_workflow_trace_payload.setdefault(key, value)
        if key in {
            "trace_unavailable",
            "trace_unavailable_reason",
            "selected_workflow_pre_trace_failure",
        }:
            completion_report_map.setdefault(key, value)


def _tool_invocation_completed_successfully(invocation: Mapping[str, Any]) -> bool:
    if bool(invocation.get("blocked")):
        return False
    if (
        isinstance(invocation.get("error"), str)
        and str(invocation.get("error")).strip()
    ):
        return False

    status = invocation.get("status")
    if isinstance(status, str) and status.strip().lower() in {
        "error",
        "failed",
        "failure",
    }:
        return False

    payload = invocation.get("effective_payload")
    if not isinstance(payload, Mapping):
        payload = invocation.get("payload")
    if isinstance(payload, Mapping):
        status_value = str(payload.get("status") or "").strip().lower()
        if status_value in {"error", "failed", "failure"}:
            return False
        if payload.get("success") is False:
            return False
    return True


def _tool_invocation_name(invocation: Mapping[str, Any]) -> str | None:
    raw_tool = invocation.get("tool")
    if isinstance(raw_tool, str) and raw_tool.strip():
        return raw_tool.strip()
    raw_method = invocation.get("method")
    if isinstance(raw_method, str) and raw_method.strip():
        return raw_method.strip()
    return None


def _tool_invocation_payload(invocation: Mapping[str, Any]) -> Any:
    for key in ("effective_payload", "result", "payload"):
        value = invocation.get(key)
        if value is not None:
            return value
    return None


def _tool_invocation_status(invocation: Mapping[str, Any]) -> str:
    raw_status = invocation.get("status")
    if isinstance(raw_status, str) and raw_status.strip():
        lowered = raw_status.strip().lower()
        if lowered in {"error", "failed", "failure"}:
            return "error"
        if lowered == "blocked":
            return "blocked"
    error_text = _safe_str(invocation.get("error"))
    if error_text:
        return "error"
    if _tool_invocation_completed_successfully(invocation):
        return "ok"
    return "error"


def _reconstruct_tool_messages_from_invocations(
    invocations: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, str]]:
    reconstructed: list[dict[str, str]] = []
    for invocation in invocations or ():
        if not isinstance(invocation, Mapping):
            continue
        tool_name = _tool_invocation_name(invocation)
        if not tool_name:
            continue
        payload: dict[str, Any] = {
            "tool": tool_name,
            "status": _tool_invocation_status(invocation),
        }
        duration_ms = invocation.get("duration_ms")
        if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
            payload["duration_ms"] = duration_ms
        result_payload = _tool_invocation_payload(invocation)
        if payload["status"] == "ok" and result_payload is not None:
            payload["payload"] = _bounded_snapshot(result_payload)
        error_text = _safe_str(invocation.get("error"))
        if error_text:
            payload["error"] = error_text
        reconstructed.append(
            {"role": "tool", "content": json.dumps(payload, default=str)}
        )
    return reconstructed


def _missing_required_tools_from_invocations(
    *,
    required_tools: Sequence[str],
    invocations: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    required = _dedupe_string_sequence(list(required_tools))
    if not required:
        return []

    successful_tools: set[str] = set()
    for invocation in invocations or ():
        if not isinstance(invocation, Mapping):
            continue
        if not _tool_invocation_completed_successfully(invocation):
            continue
        raw_tool = invocation.get("tool")
        if isinstance(raw_tool, str) and raw_tool.strip():
            successful_tools.add(raw_tool.strip().lower())

    return [
        tool_name
        for tool_name in required
        if tool_name.strip().lower() not in successful_tools
    ]


def _required_tools_from_workflow_required_effects_contract(
    contract: Mapping[str, Any] | None,
) -> list[str]:
    if not isinstance(contract, Mapping):
        return []
    required_effects = contract.get("required_effects")
    if not isinstance(required_effects, Sequence) or isinstance(
        required_effects, (str, bytes, bytearray)
    ):
        return []

    tools: list[str] = []
    for effect in required_effects:
        if not isinstance(effect, Mapping):
            continue
        raw_tools = effect.get("required_tools")
        if not isinstance(raw_tools, Sequence) or isinstance(
            raw_tools, (str, bytes, bytearray)
        ):
            continue
        tools.extend(
            str(tool_name).strip()
            for tool_name in raw_tools
            if isinstance(tool_name, str) and str(tool_name).strip()
        )
    return _dedupe_string_sequence(tools)


def _derive_required_effect_invocations_from_workflow_steps(
    *,
    child_outputs: Mapping[str, Any],
    required_tools: Sequence[str],
) -> list[dict[str, Any]]:
    required_lookup = {
        tool_name.strip().lower()
        for tool_name in required_tools
        if isinstance(tool_name, str) and tool_name.strip()
    }
    if not required_lookup:
        return []

    raw_envelopes = child_outputs.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY)
    if not isinstance(raw_envelopes, Sequence) or isinstance(
        raw_envelopes,
        (str, bytes, bytearray),
    ):
        return []

    context_target_payload = {
        key: child_outputs.get(key)
        for key in (
            "arxiv_id",
            "source_uri",
            "file_copy_concept_id",
            "computer_file_copy_concept_id",
            "source_file_copy_concept_id",
            "paper_concept_id",
            "concept_id",
        )
        if child_outputs.get(key) not in (None, "", [], {})
    }
    derived: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for envelope in raw_envelopes:
        if not isinstance(envelope, Mapping):
            continue
        action_id = _safe_str(envelope.get("action_id"))
        if not action_id or action_id.lower() not in required_lookup:
            continue
        raw_output_payload = envelope.get("output_payload")
        output_payload: dict[str, Any] = {}
        if isinstance(raw_output_payload, Mapping):
            for key, value in raw_output_payload.items():
                output_payload[str(key)] = value
        effective_payload = {**context_target_payload, **output_payload}
        status_text = (
            _safe_str(envelope.get("action_outcome"))
            or _safe_str(envelope.get("action_status"))
            or ""
        ).lower()
        raw_diagnostics = envelope.get("diagnostics")
        diagnostics: Mapping[str, Any] = {}
        if isinstance(raw_diagnostics, Mapping):
            diagnostics = raw_diagnostics
        error_text = _safe_str(diagnostics.get("error"))
        status = "ok" if status_text in {"success", "succeeded", "ok"} else "failed"
        fingerprint = (
            action_id.lower(),
            json.dumps(effective_payload, sort_keys=True, default=str),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        record: dict[str, Any] = {
            "tool": action_id,
            "status": status,
            "payload": _bounded_snapshot(effective_payload),
            "effective_payload": effective_payload,
            "workflow_step_evidence": True,
            "workflow_id": _safe_str(envelope.get("workflow_id")),
            "workflow_state_id": _safe_str(envelope.get("state_id")),
        }
        if error_text:
            record["error"] = error_text
        derived.append(record)
    return derived


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
    if lowered.startswith("execution status:"):
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


def _looks_like_count_only_result_summary(text: Any) -> bool:
    candidate = _coerce_non_empty_text(text)
    if not candidate:
        return False
    return bool(
        re.fullmatch(
            r"\d+\s+(?:result|results|item|items|concept|concepts|"
            r"relation hit|relation hits|predicate group|predicate groups|"
            r"related evidence result|related evidence results)",
            candidate.strip().lower(),
        )
    )


def _sanitise_user_response_candidate(text: Any) -> str | None:
    candidate = _coerce_non_empty_text(text)
    if not candidate:
        return None

    execution_status_match = re.search(r"(?m)^\s*Execution status:", candidate)
    if execution_status_match:
        candidate = candidate[: execution_status_match.start()].rstrip()
        if not candidate:
            return None

    if _looks_like_execution_bookkeeping_response(candidate):
        return None
    if _looks_like_count_only_result_summary(candidate):
        return None
    return candidate


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


_SNAPSHOT_SECRET_KEY_PARTS: tuple[str, ...] = (
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)
_SNAPSHOT_SALIENCE_KEY_FIELDS: tuple[str, ...] = (
    "name",
    "key",
    "field",
    "field_name",
    "header",
    "label",
    "type",
)
_SNAPSHOT_SALIENT_FIELD_NAMES: set[str] = {
    "author",
    "bcc",
    "cc",
    "date",
    "from",
    "label",
    "labels",
    "message-id",
    "message_id",
    "recipient",
    "reply-to",
    "sender",
    "snippet",
    "status",
    "subject",
    "summary",
    "title",
    "to",
}


def _snapshot_key_is_sensitive(key: str | None) -> bool:
    lowered = str(key or "").strip().lower()
    return bool(lowered) and any(part in lowered for part in _SNAPSHOT_SECRET_KEY_PARTS)


def _snapshot_scalar_value(value: Any, *, max_string_length: int) -> Any | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return (
            value[:max_string_length] + "..."
            if len(value) > max_string_length
            else value
        )
    return None


def _bounded_scalar_mapping(
    value: Mapping[str, Any],
    *,
    max_items: int,
    max_string_length: int,
) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    scalar_seen = 0
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            continue
        if scalar_seen >= max_items:
            break
        if _snapshot_key_is_sensitive(key):
            bounded[key] = "[redacted]"
            scalar_seen += 1
            continue
        scalar = _snapshot_scalar_value(item, max_string_length=max_string_length)
        if scalar is None:
            continue
        bounded[key] = scalar
        scalar_seen += 1

    if bounded:
        omitted = max(0, len(value) - len(bounded))
        if omitted:
            bounded["_omitted_non_scalar_or_excess_items"] = omitted
        return bounded
    return {"_truncated": "mapping"}


def _mapping_salience_name(value: Mapping[str, Any]) -> str | None:
    for key in _SNAPSHOT_SALIENCE_KEY_FIELDS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return None


def _is_salient_scalar_mapping(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    name = _mapping_salience_name(value)
    if name and name in _SNAPSHOT_SALIENT_FIELD_NAMES:
        return True
    return any(
        key in value
        for key in (
            "sender",
            "from",
            "subject",
            "date",
            "summary",
            "title",
            "snippet",
            "status",
        )
    )


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
            return _bounded_scalar_mapping(
                value,
                max_items=max_items,
                max_string_length=max_string_length,
            )
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
            if _snapshot_key_is_sensitive(key):
                bounded[key] = "[redacted]"
                count += 1
                continue
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
        items = (
            list(value[:max_items])
            if not isinstance(value, list)
            else value[:max_items]
        )
        if len(value) > max_items:
            retained_ids = {id(item) for item in items}
            salient_extras: list[Any] = []
            for item in value[max_items:]:
                if id(item) in retained_ids or not _is_salient_scalar_mapping(item):
                    continue
                salient_extras.append(item)
                retained_ids.add(id(item))
                if len(salient_extras) >= max_items:
                    break
            items.extend(salient_extras)
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
            omitted_count = max(0, len(value) - len(items))
            if omitted_count:
                bounded_items.append(f"... (+{omitted_count} more)")
        return bounded_items

    text = repr(value)
    return text[:max_string_length] + "..." if len(text) > max_string_length else text


_WORKFLOW_EXECUTION_TRACE_SUMMARY_KEYS: tuple[str, ...] = (
    "schema_version",
    "workflow_id",
    "completed",
    "effective_completed",
    "terminal_status",
    "final_state",
    "error",
    "response_text",
    "completion_gate_safe_to_claim_completion",
    "completion_gate_blocking_reason_codes",
    "terminal_success_contract",
    "terminal_success_evaluation",
    "workflow_required_effects_contract_id",
    "workflow_required_effects_contract_source",
    "workflow_required_effects_declared_count",
    "step_result_envelope_count",
    "action_started_count",
    "action_completed_count",
    "action_success_count",
    "action_failure_count",
    "action_unknown_count",
    "first_failing_state_id",
    "first_failing_action_id",
    "runtime_event_count",
    "terminal_effect_count",
    "terminal_effects",
    "durable_side_effect_count",
    "durable_side_effects",
)


def _build_selected_workflow_trace_execution_summary(
    *payloads: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    summary: dict[str, Any] = {}
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for key in _WORKFLOW_EXECUTION_TRACE_SUMMARY_KEYS:
            if key in summary or key not in payload:
                continue
            summary[key] = payload.get(key)
    return summary or None


def _resolve_turn_expected_outcome_contract(
    *sources: Any,
) -> TurnExpectedOutcomeContract:
    resolved_sources: list[TurnExpectedOutcomeContract | Mapping[str, Any] | None] = []
    for source in sources:
        if isinstance(source, TurnExpectedOutcomeContract):
            resolved_sources.append(source)
            continue
        if not isinstance(source, Mapping):
            resolved_sources.append(TurnExpectedOutcomeContract.from_mapping(source))
            continue
        resolved_sources.extend(
            (
                TurnExpectedOutcomeContract.from_mapping(
                    source.get("turn_expected_outcome_contract_state"),
                    source="turn_expected_outcome_contract_state",
                ),
                TurnExpectedOutcomeContract.from_mapping(
                    source.get("turn_expected_outcome_contract"),
                    source="turn_expected_outcome_contract",
                ),
                TurnExpectedOutcomeContract.from_mapping(
                    source.get("expected_outcome_contract_state"),
                    source="expected_outcome_contract_state",
                ),
                TurnExpectedOutcomeContract.from_mapping(
                    source.get("expected_outcome_contract"),
                    source="expected_outcome_contract",
                ),
                TurnExpectedOutcomeContract.from_mapping(
                    source,
                    source="context_mapping",
                ),
            )
        )
    return TurnExpectedOutcomeContract.merge_preferred(*resolved_sources)


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
                f"{len(items)} item{'s' if len(items) != 1 else ''}" if items else None
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
                candidate = _sanitise_user_response_candidate(
                    result_preview.get(field_name)
                )
                if not candidate or _looks_like_machine_json_text(candidate):
                    continue
                lowered = candidate.lower()
                if lowered in seen_candidates:
                    continue
                seen_candidates.add(lowered)
                candidate_texts.append(candidate)
        result_summary = _sanitise_user_response_candidate(
            invocation.get("result_summary")
        )
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


def _normalise_unresolved_required_preconditions(
    *,
    completion_gate_payload: Mapping[str, Any],
    record: Mapping[str, Any],
    fallback_payloads: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    evidence_payload = completion_gate_payload.get("evidence_payload")
    evidence_payload_map = (
        evidence_payload if isinstance(evidence_payload, Mapping) else {}
    )

    unresolved_preconditions: list[dict[str, Any]] = []
    raw_unresolved = evidence_payload_map.get("unresolved_preconditions")
    if isinstance(raw_unresolved, list):
        for item in raw_unresolved:
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

    if unresolved_preconditions:
        return unresolved_preconditions

    required_effect_sources: list[Any] = [record.get("required_effects")]
    for payload in fallback_payloads:
        required_effect_sources.append(payload.get("required_effects"))

    for source in required_effect_sources:
        if not isinstance(source, list):
            continue
        for effect in source:
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
        if unresolved_preconditions:
            break

    return unresolved_preconditions


def _build_fail_closed_completion_gate_response(
    *,
    combined_data: Mapping[str, Any],
    fallback_payloads: Sequence[Mapping[str, Any]],
) -> str | None:
    """Return a truthful response when completion-gate safety blocks the answer."""

    record_raw = combined_data.get("turn_execution_record")
    record = record_raw if isinstance(record_raw, Mapping) else {}
    gate_raw = record.get("completion_gate")
    gate = gate_raw if isinstance(gate_raw, Mapping) else {}

    safe_to_claim = _coerce_bool(
        gate.get(
            "safe_to_claim_completion",
            combined_data.get("completion_gate_safe_to_claim_completion"),
        ),
        default=True,
    )
    requires_follow_up = _coerce_bool(
        gate.get(
            "requires_follow_up",
            combined_data.get("completion_gate_requires_follow_up"),
        ),
        default=False,
    )
    if safe_to_claim and not requires_follow_up:
        return None

    existing_response = _coerce_non_empty_text(
        combined_data.get("final_response")
        or combined_data.get("response_text")
        or combined_data.get("current_response")
    )
    if existing_response and existing_response.startswith("Execution status:"):
        return existing_response

    unresolved_preconditions = _normalise_unresolved_required_preconditions(
        completion_gate_payload=gate,
        record=record,
        fallback_payloads=fallback_payloads,
    )

    unresolved_effect_types = {
        effect_type
        for effect_type in (
            _safe_str(unresolved.get("effect_type"))
            for unresolved in unresolved_preconditions
            if isinstance(unresolved, Mapping)
        )
        if effect_type
    }

    decision = _safe_str(gate.get("decision")) or _safe_str(
        combined_data.get("completion_gate_decision")
    )
    decision_reason = _safe_str(gate.get("decision_reason")) or _safe_str(
        combined_data.get("completion_gate_decision_reason")
    )

    if unresolved_effect_types == {"tool_execution"}:
        if decision == "failed":
            status_line = "Execution status: planned tool execution did not complete successfully."
        else:
            status_line = "Execution status: required tool execution was not completed."
    elif unresolved_effect_types == {"workflow_execution"}:
        status_line = "Execution status: selected workflow execution did not complete successfully."
    elif unresolved_effect_types and all(
        _is_evidence_effect_type(effect_type) for effect_type in unresolved_effect_types
    ):
        status_line = "Execution status: required grounded evidence was not retrieved."
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

    blocking_effect_ids = _normalise_string_list(gate.get("blocking_effect_ids"))
    if not blocking_effect_ids:
        blocking_effect_ids = _normalise_string_list(
            combined_data.get("completion_gate_blocking_effect_ids")
        )
    if blocking_effect_ids:
        status_line = (
            f"{status_line} Blocking effect IDs: {', '.join(blocking_effect_ids)}."
        )

    failure_codes = _normalise_string_list(gate.get("blocking_failure_codes"))
    if not failure_codes:
        failure_codes = _normalise_string_list(
            combined_data.get("completion_gate_blocking_failure_codes")
        )
    for unresolved in unresolved_preconditions:
        if not isinstance(unresolved, Mapping):
            continue
        for code in _normalise_string_list(unresolved.get("failure_codes")):
            if code not in failure_codes:
                failure_codes.append(code)
    if failure_codes:
        status_line = f"{status_line} Failure codes: {', '.join(failure_codes[:3])}."

    return status_line


def _required_tools_from_turn_context(data: Mapping[str, Any]) -> list[str]:
    tools: list[str] = []
    tools.extend(_dedupe_string_sequence(data.get("turn_expected_required_tools")))
    for surface in (
        data,
        data.get("completion_report"),
        data.get("selected_workflow_trace"),
    ):
        if not isinstance(surface, Mapping):
            continue
        tools.extend(_dedupe_string_sequence(surface.get("required_prompt_tools")))
        tools.extend(_dedupe_string_sequence(surface.get("missing_prompt_tools")))

    for key in (
        "turn_expected_outcome_contract_state",
        "turn_expected_outcome_contract",
        "expected_outcome_contract_state",
        "expected_outcome_contract",
    ):
        contract = data.get(key)
        if isinstance(contract, Mapping):
            tools.extend(_dedupe_string_sequence(contract.get("required_tools")))

    return _dedupe_string_sequence(tools)


def _completion_gate_record_lacks_required_tool_effects(
    *,
    record: Mapping[str, Any],
    data: Mapping[str, Any],
) -> bool:
    required_tools = _required_tools_from_turn_context(data)
    if not required_tools:
        return False

    required_effects = record.get("required_effects")
    if isinstance(required_effects, list) and required_effects:
        return False

    gate = record.get("completion_gate")
    if not isinstance(gate, Mapping):
        return True

    evidence = gate.get("evidence_payload")
    required_effect_count = 0
    if isinstance(evidence, Mapping):
        try:
            required_effect_count = int(evidence.get("required_effect_count") or 0)
        except Exception:
            required_effect_count = 0

    safe_to_claim = _coerce_bool(
        gate.get("safe_to_claim_completion"),
        default=True,
    )
    requires_follow_up = _coerce_bool(
        gate.get("requires_follow_up"),
        default=False,
    )
    return required_effect_count <= 0 and safe_to_claim and not requires_follow_up


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
    subworkflow_result = child_outputs_map.get("result")
    subworkflow_result_map = (
        dict(subworkflow_result) if isinstance(subworkflow_result, Mapping) else {}
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
        subworkflow_result_map,
        child_outputs_map,
    ):
        if isinstance(payload, Mapping):
            for key, value in payload.items():
                if isinstance(key, str) and key not in combined_data:
                    combined_data[key] = value

    artefact_lines = _render_selected_workflow_artefact_lines(combined_data)
    fail_closed_response = _build_fail_closed_completion_gate_response(
        combined_data=combined_data,
        fallback_payloads=(
            child_outputs_map,
            completion_report_map,
            workflow_execution_summary_map,
            child_snapshot,
            orchestrator_result_map,
        ),
    )
    if fail_closed_response:
        return fail_closed_response

    failure_text = (
        _coerce_non_empty_text(failure_detail) if not child_completed else None
    )
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
        (subworkflow_result_map, "response_text"),
        (subworkflow_result_map, "final_response"),
        (subworkflow_result_map, "summary"),
        (child_snapshot, "response_text"),
        (child_snapshot, "final_response"),
        (completion_report_map, "response_text"),
        (workflow_execution_summary_map, "response_text"),
    ):
        candidate = _sanitise_user_response_candidate(payload.get(field_name))
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
        if artefact_lines:
            return "\n".join([*artefact_lines, "", candidate_text])
        return candidate_text

    invocation_records = child_outputs_map.get("invocations")
    if isinstance(invocation_records, Sequence) and not isinstance(
        invocation_records, (str, bytes, bytearray)
    ):
        derived_tool_response = _sanitise_user_response_candidate(
            _derive_tool_batch_user_response(invocation_records)
        )
        if derived_tool_response:
            if artefact_lines:
                return "\n".join([*artefact_lines, "", derived_tool_response])
            return derived_tool_response

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
    turn_expected_outcome_contract: Mapping[str, Any] | None = None,
    workflow_routing: Mapping[str, Any] | None = None,
    workflow_discovery: Mapping[str, Any] | None = None,
    parent_aux_llm_calls: Sequence[Mapping[str, Any]] | None = None,
    parent_llm_calls: Sequence[Mapping[str, Any]] | None = None,
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
    resolved_turn_expected_outcome_contract = _resolve_turn_expected_outcome_contract(
        turn_expected_outcome_contract,
        selected_workflow_trace,
        child_outputs_map,
    )
    turn_expected_outcome_contract_payload = (
        resolved_turn_expected_outcome_contract.to_dict()
    )
    turn_expected_outcome_contract_available = (
        not resolved_turn_expected_outcome_contract.is_empty()
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
    if turn_expected_outcome_contract_available:
        completion_report_map["turn_expected_outcome_contract"] = dict(
            turn_expected_outcome_contract_payload
        )
        completion_report_map["turn_expected_outcome_contract_state"] = (
            resolved_turn_expected_outcome_contract.to_state_payload()
        )

    final_response = derived_user_response
    current_response = derived_user_response
    response_text = derived_user_response

    selected_workflow_trace_payload = (
        {
            str(key): value
            for key, value in selected_workflow_trace.items()
            if isinstance(key, str)
        }
        if isinstance(selected_workflow_trace, Mapping)
        else {}
    )
    if turn_expected_outcome_contract_available:
        selected_workflow_trace_payload["expected_outcome_contract"] = dict(
            turn_expected_outcome_contract_payload
        )
        selected_workflow_trace_payload["expected_outcome_contract_state"] = (
            resolved_turn_expected_outcome_contract.to_state_payload()
        )
    trace_execution_summary = _build_selected_workflow_trace_execution_summary(
        (
            child_outputs_map.get("workflow_execution_summary")
            if isinstance(child_outputs_map.get("workflow_execution_summary"), Mapping)
            else None
        ),
        completion_report_map,
    )
    if trace_execution_summary:
        selected_workflow_trace_payload["workflow_execution_summary"] = (
            trace_execution_summary
        )

    workflow_required_effects_contract = child_outputs_map.get(
        "workflow_required_effects_contract"
    )
    workflow_required_effects_contract_payload = (
        {
            str(key): value
            for key, value in workflow_required_effects_contract.items()
            if isinstance(key, str)
        }
        if isinstance(workflow_required_effects_contract, Mapping)
        else None
    )
    workflow_required_effects_contract_source = _safe_str(
        child_outputs_map.get("workflow_required_effects_contract_source")
    )
    workflow_required_effects_contract_id = _safe_str(
        child_outputs_map.get("workflow_required_effects_contract_id")
    ) or (
        _safe_str(workflow_required_effects_contract_payload.get("contract_id"))
        if isinstance(workflow_required_effects_contract_payload, Mapping)
        else None
    )
    if workflow_required_effects_contract_payload:
        completion_report_map["workflow_required_effects_contract"] = dict(
            workflow_required_effects_contract_payload
        )
        selected_workflow_trace_payload["workflow_required_effects_contract"] = dict(
            workflow_required_effects_contract_payload
        )
        if workflow_required_effects_contract_id:
            completion_report_map["workflow_required_effects_contract_id"] = (
                workflow_required_effects_contract_id
            )
            selected_workflow_trace_payload["workflow_required_effects_contract_id"] = (
                workflow_required_effects_contract_id
            )
        if workflow_required_effects_contract_source:
            completion_report_map["workflow_required_effects_contract_source"] = (
                workflow_required_effects_contract_source
            )
            selected_workflow_trace_payload[
                "workflow_required_effects_contract_source"
            ] = workflow_required_effects_contract_source

    outputs: dict[str, Any] = {
        "completion_report": completion_report_map,
        "selected_workflow_trace": {
            **selected_workflow_trace_payload,
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
    child_allowed_tools = _dedupe_string_sequence(
        child_outputs_map.get("llm_allowed_tools")
    )
    workflow_required_tools = _filter_string_sequence_to_allowed(
        _required_tools_from_workflow_required_effects_contract(
            workflow_required_effects_contract_payload
        ),
        child_allowed_tools,
    )
    derived_workflow_step_invocations = (
        _derive_required_effect_invocations_from_workflow_steps(
            child_outputs=child_outputs_map,
            required_tools=workflow_required_tools,
        )
    )
    child_invocation_maps = _merge_mapping_sequences(
        child_invocations if isinstance(child_invocations, list) else [],
        derived_workflow_step_invocations,
    )
    if child_invocation_maps:
        outputs["invocations"] = list(child_invocation_maps)
    contract_required_tools = _filter_string_sequence_to_allowed(
        resolved_turn_expected_outcome_contract.required_tools,
        child_allowed_tools,
    )
    contract_missing_tools = _missing_required_tools_from_invocations(
        required_tools=contract_required_tools,
        invocations=child_invocation_maps,
    )
    workflow_missing_tools = _missing_required_tools_from_invocations(
        required_tools=workflow_required_tools,
        invocations=child_invocation_maps,
    )
    child_tool_messages = child_outputs_map.get("tool_messages")
    if isinstance(child_tool_messages, list):
        outputs["tool_messages"] = list(child_tool_messages)
    elif child_invocation_maps:
        reconstructed_tool_messages = _reconstruct_tool_messages_from_invocations(
            child_invocation_maps
        )
        if reconstructed_tool_messages:
            outputs["tool_messages"] = reconstructed_tool_messages
    child_aux_llm_calls = child_outputs_map.get("aux_llm_calls")
    if _is_mapping_sequence(parent_aux_llm_calls) or _is_mapping_sequence(
        child_aux_llm_calls
    ):
        outputs["aux_llm_calls"] = _merge_mapping_sequences(
            parent_aux_llm_calls,
            child_aux_llm_calls,
        )
    selected_workflow_trace_map = outputs.get("selected_workflow_trace")
    if isinstance(selected_workflow_trace_map, dict):
        _merge_selected_child_trace_bridge(
            selected_workflow_trace_payload=selected_workflow_trace_payload,
            selected_workflow_trace_map=selected_workflow_trace_map,
            completion_report_map=completion_report_map,
            selected_workflow_id=clean_selected_workflow_id,
            child_completed=bool(child_completed),
            final_state=_safe_str(final_state),
            failure_detail=_safe_str(failure_detail),
            aux_entries=(
                outputs.get("aux_llm_calls")
                if _is_mapping_sequence(outputs.get("aux_llm_calls"))
                else _merge_mapping_sequences(parent_aux_llm_calls, child_aux_llm_calls)
            ),
        )
    child_llm_calls = child_outputs_map.get("llm_calls")
    if _is_mapping_sequence(parent_llm_calls) or _is_mapping_sequence(child_llm_calls):
        outputs["llm_calls"] = _merge_mapping_sequences(
            parent_llm_calls,
            child_llm_calls,
        )
    for key in (
        "required_prompt_tools",
        "required_prompt_fetch_concept_ids",
        "required_prompt_read_file_copy_ids",
        "required_prompt_scholarly_representation_for_file_copy_ids",
        "missing_prompt_tools",
        "missing_prompt_fetch_concept_ids",
        "missing_prompt_read_file_copy_ids",
        "missing_prompt_scholarly_representation_for_file_copy_ids",
        "llm_allowed_tools",
    ):
        value = child_outputs_map.get(key)
        if isinstance(value, list):
            outputs[key] = list(value)
    if contract_required_tools or workflow_required_tools:
        existing_required_prompt_tools = outputs.get("required_prompt_tools")
        existing_missing_prompt_tools = outputs.get("missing_prompt_tools")
        if child_allowed_tools:
            existing_required_prompt_tools = _filter_string_sequence_to_allowed(
                existing_required_prompt_tools,
                child_allowed_tools,
            )
            existing_missing_prompt_tools = _filter_string_sequence_to_allowed(
                existing_missing_prompt_tools,
                child_allowed_tools,
            )
        outputs["required_prompt_tools"] = _dedupe_string_sequence(
            [
                *(existing_required_prompt_tools or []),
                *contract_required_tools,
                *workflow_required_tools,
            ]
            if isinstance(existing_required_prompt_tools, list)
            else [*contract_required_tools, *workflow_required_tools]
        )
        derived_missing_prompt_tools = _dedupe_string_sequence(
            [
                *(existing_missing_prompt_tools or []),
                *contract_missing_tools,
                *workflow_missing_tools,
            ]
            if isinstance(existing_missing_prompt_tools, list)
            else [*contract_missing_tools, *workflow_missing_tools]
        )
        outputs["missing_prompt_tools"] = derived_missing_prompt_tools
        if derived_missing_prompt_tools and not _safe_str(
            outputs.get("missing_tool_call_retry_reason_override")
        ):
            reason_source = (
                "workflow required-effect"
                if workflow_missing_tools and not contract_missing_tools
                else (
                    "turn/workflow contract"
                    if workflow_missing_tools
                    else "turn contract"
                )
            )
            outputs["missing_tool_call_retry_reason_override"] = (
                f"{reason_source} required tool(s) not yet invoked successfully: "
                + ", ".join(derived_missing_prompt_tools)
            )
        completion_report_map["required_prompt_tools"] = list(
            outputs["required_prompt_tools"]
        )
        completion_report_map["missing_prompt_tools"] = list(
            outputs["missing_prompt_tools"]
        )
        selected_workflow_trace_payload["required_prompt_tools"] = list(
            outputs["required_prompt_tools"]
        )
        selected_workflow_trace_payload["missing_prompt_tools"] = list(
            outputs["missing_prompt_tools"]
        )
        selected_workflow_trace_map = outputs.get("selected_workflow_trace")
        if isinstance(selected_workflow_trace_map, dict):
            selected_workflow_trace_map["required_prompt_tools"] = list(
                outputs["required_prompt_tools"]
            )
            selected_workflow_trace_map["missing_prompt_tools"] = list(
                outputs["missing_prompt_tools"]
            )
    for key in (
        "prompt_requirement_url_policy",
        "tool_plan_context_lineage",
        "tool_follow_up_context_lineage",
    ):
        value = child_outputs_map.get(key)
        if isinstance(value, Mapping):
            outputs[key] = {
                str(item_key): item_value
                for item_key, item_value in value.items()
                if isinstance(item_key, str)
            }
    for key in (
        "required_prompt_url_extraction_tool",
        "required_prompt_url_extraction_url",
        "required_prompt_create_type_name",
        "missing_tool_call_retry_reason_override",
    ):
        value = child_outputs_map.get(key)
        if isinstance(value, str) and value.strip():
            outputs[key] = value
    if isinstance(
        child_outputs_map.get("prompt_requirements_preflight_completed"), bool
    ):
        outputs["prompt_requirements_preflight_completed"] = bool(
            child_outputs_map.get("prompt_requirements_preflight_completed")
        )
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
    if workflow_required_effects_contract_payload:
        outputs["workflow_required_effects_contract"] = dict(
            workflow_required_effects_contract_payload
        )
    if workflow_required_effects_contract_source:
        outputs["workflow_required_effects_contract_source"] = (
            workflow_required_effects_contract_source
        )
    if workflow_required_effects_contract_id:
        outputs["workflow_required_effects_contract_id"] = (
            workflow_required_effects_contract_id
        )
    outputs.update(
        build_turn_expected_outcome_boundary_payload(
            resolved_turn_expected_outcome_contract
        )
    )
    return outputs


def build_turn_recovery_tool_batch_outputs(
    *,
    requested_tool_calls: Sequence[Mapping[str, Any]],
    invocation_records: Sequence[Mapping[str, Any]],
    existing_invocations: Sequence[Mapping[str, Any]] | None = None,
    existing_tool_messages: Sequence[Mapping[str, Any]] | None = None,
    selected_workflow_trace: Mapping[str, Any] | None = None,
    selected_workflow_id: str | None = None,
    turn_expected_outcome_contract: Mapping[str, Any] | None = None,
    reasoning: str | None = None,
    omitted_call_count: int = 0,
) -> dict[str, Any]:
    """Build the canonical recovery-tool-batch context surface.

    Keep this payload bounded and user-facing enough for narration while making
    the direct recovery action schema visible to later diagnostics.
    """

    clean_selected_workflow_id = _safe_str(selected_workflow_id)
    bounded_tool_calls = _bounded_snapshot(list(requested_tool_calls), max_items=4)
    bounded_invocations = _bounded_snapshot(
        list(invocation_records), max_depth=5, max_items=8
    )
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
    resolved_turn_expected_outcome_contract = _resolve_turn_expected_outcome_contract(
        turn_expected_outcome_contract,
        selected_workflow_trace,
    )
    turn_expected_outcome_contract_payload = (
        resolved_turn_expected_outcome_contract.to_dict()
    )
    turn_expected_outcome_contract_available = (
        not resolved_turn_expected_outcome_contract.is_empty()
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
    if turn_expected_outcome_contract_available:
        execution_payload["turn_expected_outcome_contract"] = dict(
            turn_expected_outcome_contract_payload
        )
        execution_payload["turn_expected_outcome_contract_state"] = (
            resolved_turn_expected_outcome_contract.to_state_payload()
        )

    batch_tool_messages = [
        {"role": "tool", "content": _render_tool_batch_message_content(record)}
        for record in invocation_records
        if isinstance(record, Mapping)
    ]
    combined_invocations = [
        {str(key): value for key, value in item.items() if isinstance(key, str)}
        for item in (existing_invocations or [])
        if isinstance(item, Mapping)
    ] + [
        {str(key): value for key, value in item.items() if isinstance(key, str)}
        for item in invocation_records
        if isinstance(item, Mapping)
    ]
    combined_tool_messages = [
        {str(key): value for key, value in item.items() if isinstance(key, str)}
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
    if turn_expected_outcome_contract_available:
        merged_selected_workflow_trace["expected_outcome_contract"] = dict(
            turn_expected_outcome_contract_payload
        )
        merged_selected_workflow_trace["expected_outcome_contract_state"] = (
            resolved_turn_expected_outcome_contract.to_state_payload()
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
    outputs.update(
        build_turn_expected_outcome_boundary_payload(
            resolved_turn_expected_outcome_contract
        )
    )
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
    request_inputs = request.inputs if isinstance(request.inputs, Mapping) else {}
    emit_default_critic_verdict = _coerce_bool(
        request_inputs.get("emit_default_critic_verdict"),
        default=True,
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
        turn_expected_outcome_contract=(
            data.get("turn_expected_outcome_contract_state")
            or data.get("turn_expected_outcome_contract")
            or (
                data.get("selected_workflow_trace", {}).get(
                    "expected_outcome_contract_state"
                )
                if isinstance(data.get("selected_workflow_trace"), Mapping)
                else None
            )
            or (
                data.get("selected_workflow_trace", {}).get("expected_outcome_contract")
                if isinstance(data.get("selected_workflow_trace"), Mapping)
                else None
            )
        ),
        critic_verdict=data.get("critic_verdict"),
        completion_gate_verdict=data.get("completion_gate_verdict"),
        completion_report=data.get("completion_report"),
        required_prompt_tools=(
            data.get("required_prompt_tools")
            if isinstance(data.get("required_prompt_tools"), list)
            else None
        ),
        required_tool_obligation_ledger=(
            data.get("required_tool_obligation_ledger")
            if isinstance(data.get("required_tool_obligation_ledger"), Mapping)
            else None
        ),
    )

    completion_gate = turn_execution_record.get("completion_gate")
    gate_requires_follow_up = False
    gate_decision = None
    gate_safe_to_claim = True
    if isinstance(completion_gate, Mapping):
        gate_requires_follow_up = bool(completion_gate.get("requires_follow_up", False))
        raw_gate_decision = completion_gate.get("decision")
        if isinstance(raw_gate_decision, str) and raw_gate_decision.strip():
            gate_decision = raw_gate_decision.strip()
        gate_safe_to_claim = bool(completion_gate.get("safe_to_claim_completion", True))

    critic_summary: Mapping[str, Any] = {}
    critic_payload = turn_execution_record.get("critic")
    if isinstance(critic_payload, Mapping):
        raw_summary = critic_payload.get("summary")
        if isinstance(raw_summary, Mapping):
            critic_summary = dict(raw_summary)

    unresolved_check_count = (
        int(critic_summary.get("not_verified_count") or 0)
        + int(critic_summary.get("inconclusive_count") or 0)
        + int(critic_summary.get("error_count") or 0)
    )
    critic_verdict_payload: dict[str, Any] | None = None
    if isinstance(critic_payload, Mapping):
        raw_verdict = critic_payload.get("verdict")
        if isinstance(raw_verdict, Mapping):
            critic_verdict_payload = {
                str(key): value
                for key, value in raw_verdict.items()
                if isinstance(key, str)
            }
        elif emit_default_critic_verdict:
            critic_verdict_payload = {}
        if isinstance(critic_verdict_payload, dict):
            critic_verdict_payload.setdefault(
                "workflow_id", KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID
            )
            critic_verdict_payload.setdefault("summary", dict(critic_summary))
            critic_verdict_payload.setdefault(
                "has_unresolved_checks", unresolved_check_count > 0
            )
            critic_verdict_payload.setdefault(
                "unresolved_check_count", unresolved_check_count
            )
            turn_execution_record["critic"] = {
                **critic_payload,
                "verdict": dict(critic_verdict_payload),
            }

    required_effects = turn_execution_record.get("required_effects")
    if not isinstance(required_effects, list):
        required_effects = []
    postcondition_checks = turn_execution_record.get("postcondition_checks")
    if not isinstance(postcondition_checks, list):
        postcondition_checks = []

    execution_payload = turn_execution_record.get("execution")
    execution_payload_map = (
        dict(execution_payload) if isinstance(execution_payload, Mapping) else {}
    )
    turn_execution_critic_evidence_bundle = {
        "schema_version": "turn_execution_postcondition_critic_bundle.v1",
        "request_id": turn_execution_record.get("request_id"),
        "workflow_id": KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
        "prompt_text": prompt_text,
        "response_text": response_text,
        "completion_report": data.get("completion_report"),
        "turn_expected_outcome_contract_state": turn_execution_record.get(
            "turn_expected_outcome_contract_state"
        ),
        "selected_workflow_trace": _bounded_snapshot(
            execution_payload_map.get("selected_workflow_trace"),
            max_depth=4,
            max_items=8,
            max_string_length=600,
        ),
        "required_effects": _bounded_snapshot(
            list(required_effects),
            max_depth=4,
            max_items=8,
            max_string_length=600,
        ),
        "postcondition_checks": _bounded_snapshot(
            list(postcondition_checks),
            max_depth=4,
            max_items=8,
            max_string_length=600,
        ),
        "critic_summary": dict(critic_summary),
        "search_evidence": _bounded_snapshot(
            execution_payload_map.get("search_evidence"),
            max_depth=4,
            max_items=6,
            max_string_length=600,
        ),
        "required_prompt_tools": _bounded_snapshot(
            execution_payload_map.get("required_prompt_tools"),
            max_depth=3,
            max_items=8,
            max_string_length=200,
        ),
        "workflow_selection": _bounded_snapshot(
            turn_execution_record.get("workflow_selection"),
            max_depth=4,
            max_items=8,
            max_string_length=400,
        ),
        "workflow_routing_diagnostics": _bounded_snapshot(
            turn_execution_record.get("workflow_routing_diagnostics"),
            max_depth=4,
            max_items=8,
            max_string_length=400,
        ),
    }

    if isinstance(raw_aux, list) and isinstance(critic_verdict_payload, dict):
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
            "turn_execution_critic_evidence_bundle": turn_execution_critic_evidence_bundle,
            "critic_summary": dict(critic_summary),
            "critic_verdict": (
                dict(critic_verdict_payload)
                if isinstance(critic_verdict_payload, dict)
                else None
            ),
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
    record_is_stale = bool(
        isinstance(record, Mapping)
        and _completion_gate_record_lacks_required_tool_effects(
            record=record,
            data=data,
        )
    )
    if not isinstance(record, Mapping) or record_is_stale:
        critic_result = run_turn_execution_critic(
            request,
            annotation_component=annotation_component,
            annotation_function=annotation_function.replace(
                "completion_gate", "critic"
            ),
        )
        rebuilt_record = critic_result.outputs.get("turn_execution_record")
        record = rebuilt_record if isinstance(rebuilt_record, Mapping) else {}
        if record_is_stale:
            aux_llm_calls = data.get("aux_llm_calls")
            if isinstance(aux_llm_calls, list):
                try:
                    aux_llm_calls.append(
                        annotate_python_decision_event(
                            {
                                "type": "completion_gate_record_rebuilt",
                                "reason": "stale_zero_required_effects_with_required_tools",
                                "required_tools": _required_tools_from_turn_context(
                                    data
                                ),
                            },
                            stage="completion_gate",
                            component=annotation_component,
                            function=annotation_function,
                            decision_class="completion_gate_record_rebuild",
                            decision_source="required_tool_contract_presence",
                            changed_outcome=True,
                            reason_code="stale_zero_required_effects",
                            possible_inappropriate_python_code_use=False,
                        )
                    )
                except Exception:
                    pass

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
    requires_follow_up = bool(completion_gate_payload.get("requires_follow_up", False))
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
    loop_last_blocking_signature = (
        _safe_str(data.get("completion_gate_loop_last_blocking_signature")) or ""
    )
    current_monotonic = time.monotonic()
    loop_started_monotonic_raw = data.get("completion_gate_loop_started_monotonic")
    if isinstance(loop_started_monotonic_raw, (int, float)):
        loop_started_monotonic = float(loop_started_monotonic_raw)
    else:
        loop_started_monotonic = current_monotonic
    loop_timer_started_at_gate = bool(
        data.get("completion_gate_loop_timer_started_at_gate")
    )
    if loop_attempts == 0 and not loop_timer_started_at_gate:
        loop_started_monotonic = current_monotonic
        loop_timer_started_at_gate = True
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
                status_line = "Execution status: planned tool execution did not complete successfully."
            else:
                status_line = (
                    "Execution status: required tool execution was not completed."
                )
        elif unresolved_effect_types == {"workflow_execution"}:
            status_line = "Execution status: selected workflow execution did not complete successfully."
        elif unresolved_effect_types and all(
            _is_evidence_effect_type(effect_type)
            for effect_type in unresolved_effect_types
        ):
            status_line = (
                "Execution status: required grounded evidence was not retrieved."
            )
        elif decision == "failed":
            status_line = "Execution status: requested mutation failed or was blocked."
        elif decision == "escalation_required":
            status_line = "Execution status: requested mutation was not executed."
        elif decision == "partial":
            status_line = "Execution status: mutation may have run but verification is inconclusive."
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

        preserve_existing_user_response = bool(
            unresolved_effect_types == {"required_evidence_answer_consistency"}
            and isinstance(final_response, str)
            and final_response.strip()
        )
        replace_existing_user_response = bool(
            unresolved_effect_types
            and not preserve_existing_user_response
            and all(
                _is_evidence_effect_type(effect_type)
                for effect_type in unresolved_effect_types
            )
        )
        ledger_replaced_empty = not (
            isinstance(final_response, str) and final_response.strip()
        )
        if preserve_existing_user_response:
            pass
        elif replace_existing_user_response:
            final_response = status_line
        elif isinstance(final_response, str) and final_response.strip():
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
                            "ledger_preserved_existing_user_response": (
                                preserve_existing_user_response
                            ),
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
                " ".join(reason_parts)
                if reason_parts
                else "Required effects unresolved."
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
    recovery_repeat_eligible = bool(
        repeat_eligible and not terminal_non_repeatable and repeat_stop_reason is None
    )
    budget_diagnostics = _build_turn_execution_budget_diagnostics(
        data=data,
        environment=request.environment,
        invocation_count=invocation_count,
        completion_gate_loop_attempts=loop_attempts,
        completion_gate_loop_max_attempts=loop_max_attempts,
        completion_gate_loop_elapsed_ms=loop_elapsed_ms,
        completion_gate_loop_max_elapsed_ms=loop_max_elapsed_ms,
        completion_gate_loop_no_progress_streak=loop_no_progress_streak,
        completion_gate_loop_no_progress_limit=loop_no_progress_limit,
        completion_gate_loop_stall_elapsed_ms=loop_stall_elapsed_ms,
        completion_gate_loop_stall_max_elapsed_ms=loop_stall_max_elapsed_ms,
        completion_gate_repeat_stop_reason=repeat_stop_reason,
    )

    completion_gate_evidence_payload: dict[str, Any] = dict(record_evidence_payload)
    completion_gate_evidence_payload.update(
        {
            "decision": decision,
            "decision_reason": decision_reason,
            "safe_to_claim_completion": safe_to_claim_completion,
            "requires_follow_up": requires_follow_up,
            "repeat_eligible": recovery_repeat_eligible,
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
            "budget_diagnostics": budget_diagnostics,
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
        namespace = _safe_str(
            getattr(request.environment, "user_namespace", None)
        ) or _safe_str(data.get("namespace"))
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
        incident_text = (
            prompt_text[:500] if prompt_text else str(decision_reason or "")[:500]
        )
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
                else (
                    "already_autotriggered"
                    if already_autotriggered
                    else "no_follow_up_required"
                )
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
                        "budget_diagnostics": budget_diagnostics,
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
            "completion_gate_repeat_eligible": recovery_repeat_eligible,
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
            "completion_gate_loop_timer_started_at_gate": loop_timer_started_at_gate,
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
            "completion_gate_budget_diagnostics": budget_diagnostics,
            "execution_budget_diagnostics": budget_diagnostics,
            "completion_gate_escalation_signal": escalation_signal,
            "completion_gate_escalation_reason": escalation_reason,
            "workflow_introspection_autotrigger": introspection_autotrigger,
            "episode_evaluation_autotrigger": introspection_autotrigger,
            "result": requires_follow_up,
        }
    )
