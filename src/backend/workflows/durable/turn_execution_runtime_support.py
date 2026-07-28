"""Compatibility and evidence support for retained durable turn records.

Ordinary interactive turns no longer execute a supervising turn workflow.
This module keeps the independently useful actor, tool-identity, verification,
record-reconstruction, and explicit-workflow compatibility helpers needed by
durable history and callable workflows.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Sequence

from ...services.python_decision_authority_service import (
    annotate_python_decision_event,
)
from ...services.namespace_service import derive_actor_context_from_namespace
from ...services.tool_target_contract_validation import (
    target_contract_state_from_context,
)
from ...services.turn_execution_record_service import (
    build_turn_execution_record,
    build_verification_tool_evidence,
)
from ..action_registry import WorkflowActionResult
from ..definitions import (
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _build_current_actor_scope_evidence(
    *,
    environment: Any,
) -> dict[str, Any]:
    """Project bounded authenticated actor scope for represented evaluation."""

    user_concept_id = _safe_str(getattr(environment, "user_concept_id", None))
    org_concept_id = _safe_str(getattr(environment, "org_concept_id", None))
    namespace = _safe_str(getattr(environment, "user_namespace", None))
    namespace_user_id, namespace_org_id = derive_actor_context_from_namespace(
        namespace
    )
    user_matches = (
        user_concept_id == namespace_user_id
        if user_concept_id and namespace_user_id
        else None
    )
    organisation_matches = (
        org_concept_id == namespace_org_id
        if org_concept_id and namespace_org_id
        else None
    )
    inconsistent = user_matches is False or organisation_matches is False
    verified = bool(
        user_concept_id
        and namespace
        and user_matches is True
        and (not org_concept_id or organisation_matches is True)
    )
    authority_status = (
        "inconsistent" if inconsistent else "verified" if verified else "missing"
    )
    return {
        "schema_version": "current_actor_scope_evidence.v1",
        "authority_source": "workflow_environment",
        "authority_status": authority_status,
        "user": {"concept_id": user_concept_id} if user_concept_id else None,
        "organisation": (
            {"concept_id": org_concept_id} if org_concept_id else None
        ),
        "namespace": namespace,
        "namespace_consistency": {
            "user_matches": user_matches,
            "organisation_matches": organisation_matches,
        },
    }


def _truthy_env_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_agent_test_instance() -> bool:
    return _truthy_env_value(os.getenv("VON_AGENT_TEST_INSTANCE"))


def _resolve_method_catalogue_snapshot(
    *,
    data: Mapping[str, Any],
    environment: Any | None = None,
) -> Mapping[str, Any] | None:
    """Return the method catalogue snapshot already carried by the workflow."""

    data_catalogue = data.get("method_catalogue")
    if isinstance(data_catalogue, Mapping):
        return data_catalogue

    gateway = getattr(environment, "gateway", None)
    describe_methods = getattr(gateway, "describe_methods", None)
    if not callable(describe_methods):
        return None

    try:
        described_methods = describe_methods()
    except Exception:
        return None
    return described_methods if isinstance(described_methods, Mapping) else None


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


def _text_contains_workflow_llm_timeout(value: Any) -> bool:
    text = _safe_str(value)
    return bool(text and "workflow_llm_step_timeout" in text.lower())


def _iter_bounded_failure_surfaces(
    *payloads: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    """Return bounded nested execution surfaces that may carry failure facts."""

    nested_mapping_keys = {
        "completion_report",
        "completion_gate",
        "completion_gate_verdict",
        "evidence_payload",
        "selected_workflow_trace",
        "workflow_routing",
        "workflow_routing_diagnostics",
        "orchestrator_result",
        "last_failed_action_outputs",
        "last_workflow_step_result_envelope",
        "llm_step_envelope",
        "subworkflow_invocation",
        "subworkflow_result_envelope",
        "workflow_result_envelope",
        "result_envelope",
        "result",
    }
    nested_sequence_keys = {
        "aux_llm_calls",
        "llm_calls",
        "workflow_events",
        "runtime_events",
        "workflow_step_result_envelopes",
    }
    queue: list[tuple[Mapping[str, Any], int]] = [
        (payload, 0) for payload in payloads if isinstance(payload, Mapping)
    ]
    surfaces: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    while queue and len(surfaces) < 250:
        surface, depth = queue.pop(0)
        identity = id(surface)
        if identity in seen:
            continue
        seen.add(identity)
        surfaces.append(surface)
        if depth >= 5:
            continue
        for key in nested_mapping_keys:
            nested = surface.get(key)
            if isinstance(nested, Mapping):
                queue.append((nested, depth + 1))
        for key in nested_sequence_keys:
            nested_items = surface.get(key)
            if not isinstance(nested_items, list):
                continue
            for nested in nested_items[-50:]:
                if isinstance(nested, Mapping):
                    queue.append((nested, depth + 1))
    return surfaces


def _workflow_llm_timeout_blocker_from_turn_data(
    *,
    data: Mapping[str, Any],
    record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Detect structured workflow-LLM timeouts that make completion unsafe."""

    surfaces = _iter_bounded_failure_surfaces(data, record)

    timeout_detail: str | None = None
    timeout_stage: str | None = None
    for surface in surfaces:
        envelope = surface.get("llm_step_envelope")
        if isinstance(envelope, Mapping):
            completion_reason = (
                _safe_str(envelope.get("completion_reason")) or ""
            ).lower()
            if completion_reason == "timeout" or _text_contains_workflow_llm_timeout(
                envelope.get("timeout_detail")
            ):
                timeout_detail = (
                    _safe_str(envelope.get("timeout_detail"))
                    or "workflow_llm_step_timeout"
                )
                timeout_stage = (
                    _safe_str(envelope.get("timeout_stage"))
                    or _safe_str(envelope.get("stage"))
                    or timeout_stage
                )
                break

        for key in (
            "error",
            "failure_code",
            "failure_reason",
            "failure_detail",
            "workflow_failure_detail",
            "selected_workflow_failure_detail",
            "llm_step_error",
            "last_action_error",
            "action_error",
            "subworkflow_error",
            "child_error",
            "timeout_detail",
            "final_response",
            "response_text",
            "current_response",
        ):
            value = surface.get(key)
            if _text_contains_workflow_llm_timeout(value):
                timeout_detail = _safe_str(value)
                timeout_stage = timeout_stage or _safe_str(surface.get("stage"))
                break
        if timeout_detail:
            break

    if not timeout_detail:
        return None

    stage_suffix = f" ({timeout_stage})" if timeout_stage else ""
    decision_reason = (
        f"A workflow LLM stage{stage_suffix} timed out before execution evidence "
        "could be verified."
    )
    return {
        "effect_id": "effect_workflow_llm_timeout_1",
        "effect_type": "workflow_execution",
        "status": "not_executed",
        "status_reason": decision_reason,
        "failure_code": "workflow_llm_step_timeout",
        "failure_codes": ["workflow_llm_step_timeout"],
        "decision": "escalation_required",
        "decision_reason": decision_reason,
        "repeat_eligible": False,
        "source": "workflow_llm_timeout",
        "timeout_stage": timeout_stage,
        "timeout_detail": timeout_detail,
    }


def _bool_field(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _selected_workflow_failure_blocker_from_turn_data(
    *,
    data: Mapping[str, Any],
    record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Detect a failed selected-workflow run when no required effects exist."""

    completion_report_raw = data.get("completion_report")
    if not isinstance(completion_report_raw, Mapping) and isinstance(record, Mapping):
        completion_report_raw = record.get("completion_report")
    if not isinstance(completion_report_raw, Mapping):
        return None
    completion_report = completion_report_raw
    workflow_routing = (
        data.get("workflow_routing")
        if isinstance(data.get("workflow_routing"), Mapping)
        else {}
    )

    workflow_id = (
        _safe_str(completion_report.get("workflow_id"))
        or _safe_str(data.get("selected_workflow_id"))
        or _safe_str(workflow_routing.get("workflow_id"))
        or _safe_str(workflow_routing.get("selected_workflow_id"))
    )
    effective_completed = _bool_field(completion_report.get("effective_completed"))
    completed = _bool_field(completion_report.get("completed"))
    terminal_status = (
        _safe_str(completion_report.get("terminal_status")) or ""
    ).lower()
    final_state = (_safe_str(completion_report.get("final_state")) or "").lower()
    failure_count = _coerce_non_negative_int(
        completion_report.get("action_failure_count"),
        default=0,
        max_value=100_000,
    )
    failed_action_ids = _normalise_string_list(
        completion_report.get("failed_action_ids")
    )
    first_failing_state_id = _safe_str(completion_report.get("first_failing_state_id"))
    first_failing_action_id = _safe_str(
        completion_report.get("first_failing_action_id")
    )

    failed = bool(
        effective_completed is False
        or (completed is False and terminal_status in {"failed", "failure", "error"})
        or final_state.endswith("failed")
        or final_state in {"failed", "failure", "error"}
        or failure_count > 0
        or failed_action_ids
    )
    if not failed:
        return None

    detail_parts: list[str] = []
    if workflow_id:
        detail_parts.append(f"Selected workflow {workflow_id} did not complete safely")
    else:
        detail_parts.append("Selected workflow did not complete safely")
    if first_failing_state_id:
        detail_parts.append(f"first failing state: {first_failing_state_id}")
    if first_failing_action_id:
        detail_parts.append(f"first failing action: {first_failing_action_id}")
    elif failed_action_ids:
        detail_parts.append(f"failed actions: {', '.join(failed_action_ids[:3])}")
    decision_reason = "; ".join(detail_parts) + "."

    return {
        "effect_id": "effect_selected_workflow_execution_1",
        "effect_type": "workflow_execution",
        "status": "not_satisfied",
        "status_reason": decision_reason,
        "failure_code": "selected_workflow_execution_failed",
        "failure_codes": ["selected_workflow_execution_failed"],
        "decision": "failed",
        "decision_reason": decision_reason,
        "repeat_eligible": False,
        "source": "selected_workflow_completion_report",
        "workflow_id": workflow_id,
        "first_failing_state_id": first_failing_state_id,
        "first_failing_action_id": first_failing_action_id,
    }


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


def _coerce_non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _looks_like_machine_json_text(text: Any) -> bool:
    candidate = _coerce_non_empty_text(text)
    if not candidate:
        return False
    fenced_match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced_match:
        candidate = fenced_match.group(1).strip()
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
    if _text_contains_workflow_llm_timeout(candidate):
        return None
    if _looks_like_count_only_result_summary(candidate):
        return None
    return candidate


def coerce_user_visible_response_text(value: Any) -> str | None:
    candidate = _sanitise_user_response_candidate(value)
    if candidate and not _looks_like_machine_json_text(candidate):
        return candidate

    if isinstance(value, Mapping):
        for key in (
            "response_text",
            "final_response",
            "current_response",
            "selected_workflow_user_response",
        ):
            if key not in value:
                continue
            nested_candidate = coerce_user_visible_response_text(value.get(key))
            if nested_candidate:
                return nested_candidate
        scalar_parts: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if isinstance(item, (str, int, float, bool)) and item is not None:
                scalar_parts.append(f"{key}: {item}")
        return "; ".join(scalar_parts) if scalar_parts else None

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        lines: list[str] = []
        for index, item in enumerate(value, start=1):
            item_text = coerce_user_visible_response_text(item)
            if item_text:
                lines.append(f"{index}. {item_text}")
        return "\n".join(lines) if lines else None

    return None


_COMPLETION_LEDGER_STATUS_PREFIXES: tuple[str, ...] = (
    "Execution status: planned tool execution did not complete successfully.",
    "Execution status: required tool execution was not completed.",
    "Execution status: selected workflow execution did not complete successfully.",
    "Execution status: required grounded evidence was not retrieved.",
    "Execution status: requested mutation failed or was blocked.",
    "Execution status: requested mutation was not executed.",
    "Execution status: mutation may have run but verification is inconclusive.",
    "Execution status: follow-up verification is required.",
)


def strip_completion_ledger_suffix(text: Any) -> str:
    candidate = _safe_str(text)
    if not candidate:
        return ""
    marker_match = None
    for match in re.finditer(r"(?m)^\s*Execution status:", candidate):
        marker_match = match
    if marker_match is None:
        return candidate
    suffix = candidate[marker_match.start() :].strip()
    if not suffix.startswith(_COMPLETION_LEDGER_STATUS_PREFIXES):
        return candidate
    return candidate[: marker_match.start()].rstrip()


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


def _completion_gate_record_has_stale_required_tool_obligation_effect(
    *,
    record: Mapping[str, Any],
    data: Mapping[str, Any],
) -> bool:
    required_effects = record.get("required_effects")
    if not isinstance(required_effects, list) or not required_effects:
        return False

    stale_required_tools: list[str] = []
    for effect in required_effects:
        if not isinstance(effect, Mapping):
            continue
        effect_id = _safe_str(effect.get("effect_id"))
        intent_origin = _safe_str(effect.get("intent_origin"))
        if not (
            effect_id == "effect_required_tool_obligations_1"
            or intent_origin == "required_tool_obligation_ledger"
        ):
            continue
        effect_status = _safe_str(effect.get("status"))
        if effect_status not in {"not_executed", "not_satisfied"}:
            continue
        stale_required_tools.extend(
            _dedupe_string_sequence(effect.get("required_tools") or [])
        )

    stale_required_tools = _dedupe_string_sequence(stale_required_tools)
    if not stale_required_tools:
        return False

    raw_invocations = data.get("invocations")
    invocations = raw_invocations if isinstance(raw_invocations, list) else []
    if not invocations:
        return False

    try:
        from ...services.required_tool_obligation_service import (
            build_required_tool_obligation_ledger,
        )

        refreshed_ledger = build_required_tool_obligation_ledger(
            required_tools=stale_required_tools,
            invocations=[
                invocation
                for invocation in invocations
                if isinstance(invocation, Mapping)
            ],
            method_catalogue=_resolve_method_catalogue_snapshot(data=data),
            target_contract_state=target_contract_state_from_context(data),
        )
    except Exception:
        return False

    return (
        int(refreshed_ledger.get("required_tool_count") or 0) > 0
        and int(refreshed_ledger.get("unsatisfied_count") or 0) == 0
    )


def _completion_gate_record_rebuild_reason(
    *,
    record: Mapping[str, Any],
    data: Mapping[str, Any],
) -> str | None:
    if _completion_gate_record_lacks_required_tool_effects(record=record, data=data):
        return "stale_zero_required_effects_with_required_tools"
    if _completion_gate_record_has_stale_required_tool_obligation_effect(
        record=record,
        data=data,
    ):
        return "stale_required_tool_obligation_effect_satisfied_by_current_invocations"
    return None


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
    method_catalogue = _resolve_method_catalogue_snapshot(
        data=data,
        environment=env,
    )
    critic_verdict_input = request_inputs.get("critic_verdict")
    effective_critic_verdict = (
        data.get("critic_verdict")
        if isinstance(data.get("critic_verdict"), Mapping)
        else critic_verdict_input if isinstance(critic_verdict_input, Mapping) else None
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
        critic_verdict=effective_critic_verdict,
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
        method_catalogue=method_catalogue,
        workflow_failure_evidence=data,
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

    terminal_outcome_receipt_raw = turn_execution_record.get("terminal_outcome_receipt")
    terminal_outcome_receipt = (
        dict(terminal_outcome_receipt_raw)
        if isinstance(terminal_outcome_receipt_raw, Mapping)
        else None
    )
    terminal_outcome_receipt_validation_raw = turn_execution_record.get(
        "terminal_outcome_receipt_validation"
    )
    terminal_outcome_receipt_validation = (
        dict(terminal_outcome_receipt_validation_raw)
        if isinstance(terminal_outcome_receipt_validation_raw, Mapping)
        else {}
    )

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
        "current_actor_scope_evidence": _build_current_actor_scope_evidence(
            environment=env
        ),
        "prompt_text": prompt_text,
        "response_text": response_text,
        "completion_report": data.get("completion_report"),
        "final_answer_synthesis": _bounded_snapshot(
            turn_execution_record.get("final_answer_synthesis"),
            max_depth=8,
            max_items=10,
            max_string_length=800,
        ),
        "requested_evidence_lineage": _bounded_snapshot(
            turn_execution_record.get("requested_evidence_lineage"),
            max_depth=8,
            max_items=10,
            max_string_length=800,
        ),
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
            max_depth=6,
            max_items=12,
            max_string_length=600,
        ),
        "verification_evidence": _bounded_snapshot(
            build_verification_tool_evidence(tool_invocations),
            max_depth=6,
            max_items=12,
            max_string_length=800,
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
        "terminal_outcome_receipt": _bounded_snapshot(
            terminal_outcome_receipt,
            max_depth=6,
            max_items=16,
            max_string_length=600,
        ),
        "terminal_outcome_receipt_validation": dict(
            terminal_outcome_receipt_validation
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
            "terminal_outcome_receipt": terminal_outcome_receipt,
            "terminal_outcome_receipt_validation": (
                terminal_outcome_receipt_validation
            ),
            "completion_gate_decision": gate_decision,
            "completion_gate_requires_follow_up": gate_requires_follow_up,
            "completion_gate_safe_to_claim_completion": gate_safe_to_claim,
        }
    )
