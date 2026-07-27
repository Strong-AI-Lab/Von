"""Turn execution record builder and projection storage utilities.

This service provides a canonical `turn_execution_record.v1` payload for each
assistant turn and a query-optimised Mongo projection collection
(`turn_execution_records`) for cross-conversation failure analysis.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, cast

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

from ..db.mongo_client import get_db
from .arxiv_paper_link_service import extract_arxiv_id_candidates
from .debug_payload_store import (
    compact_debug_payload_for_storage,
    hydrate_debug_payload_blob_refs,
)
from .mongo_observability_service import (
    build_mongo_operation_comment,
    observe_mongo_operation,
)
from .representation_contract_vontology_service import (
    canonical_representation_profile_concept_ids,
    ensure_canonical_representation_contract_profiles,
    load_representation_contract_profiles_from_concept_ids,
)
from .required_tool_obligation_service import (
    BLOCKER_REQUIRED_TOOL_NOT_PLANNED,
    build_required_tool_obligation_ledger,
    required_tool_obligation_effect,
)
from .required_tool_identity_service import canonical_required_tool_key
from .tool_metadata_service import (
    is_tool_prompt_required_evidence,
    is_tool_prompt_required_mutation,
    is_tool_search_evidence,
    is_tool_verification_read,
    is_tool_write,
)
from .tool_observation_ledger_service import build_tool_observation_ledger
from .tool_target_contract_validation import target_contract_state_from_context
from .turn_decision_attribution_service import build_turn_decision_attribution
from .turn_execution_diagnostic_event_service import (
    derive_tool_observations_from_diagnostic_events,
)
from .turn_context_adjudication_projection_service import (
    build_turn_context_adjudication_projection,
)
from .turn_expected_outcome_obligation_carry_forward import (
    adjudicate_conditional_required_tool_activation,
    obligation_carry_forward_permission,
)
from ..workflows.conversation_turn_stage_model import (
    build_conversation_turn_stage_model_snapshot,
    build_conversation_turn_stage_path,
)
from ..workflows.required_effects_contracts import (
    WORKFLOW_REQUIRED_EFFECTS_CONTRACT_SCHEMA_VERSION,
)
from ..workflows.terminal_outcome_receipts import (
    apply_terminal_outcome_receipt_to_completion_gate,
    validate_terminal_outcome_receipt,
)
from ..workflows.turn_expected_outcome_contract import (
    TurnExpectedOutcomeContract,
    extract_vontology_concept_ids_from_text,
)

logger = logging.getLogger(__name__)

TURN_EXECUTION_RECORD_SCHEMA_VERSION = "turn_execution_record.v1"
TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION = "turn_execution_correctness.v1"
LATE_EFFECT_OBSERVATION_SCHEMA_VERSION = "late_effect_observation.v1"
WORKFLOW_ROUTING_DIAGNOSTICS_SCHEMA_VERSION = "workflow_routing_diagnostics.v1"
FINAL_ANSWER_SYNTHESIS_TELEMETRY_SCHEMA_VERSION = "final_answer_synthesis_telemetry.v1"
TOOL_EVIDENCE_PROJECTION_REACHABILITY_SCHEMA_VERSION = (
    "tool_evidence_projection_reachability.v1"
)
REQUESTED_EVIDENCE_LINEAGE_SCHEMA_VERSION = "requested_evidence_lineage.v1"
TURN_EXECUTION_RECORDS_COLLECTION = "turn_execution_records"

_EXECUTION_SURFACE_CONTEXT_FIELDS: tuple[str, ...] = (
    "error",
    "error_code",
    "failure_code",
    "message",
    "timeout_phase",
    "phase",
    "workflow_instance_id",
    "instance_id",
    "execution_id",
    "terminal_status",
    "final_state",
)

_FINAL_ANSWER_SYNTHESIS_PROMPT_CONCEPT_IDS = frozenset(
    {"#V#prompt_turn_execution_narrate_completion_report"}
)
_FINAL_ANSWER_SYNTHESIS_PROMPT_MARKERS = (
    "# prompt_turn_execution_narrate_completion_report",
)

_TURN_EXECUTION_INDEXES_READY = False
_TURN_EXECUTION_INDEXES_LOCK = threading.Lock()
_SEARCH_EVIDENCE_MAX_ARGUMENT_CHARS = 50_000
_SEARCH_EVIDENCE_MAX_RESULT_CHARS = 500_000
_SEARCH_EVIDENCE_PREVIEW_CHARS = 8_000
_FINAL_ANSWER_PROJECTION_PAYLOAD_MAX_DEPTH = 4
_FINAL_ANSWER_PROJECTION_PAYLOAD_MAX_ITEMS = 8
_FINAL_ANSWER_PROJECTION_PAYLOAD_MAX_STRING_CHARS = 700
_FINAL_ANSWER_PUBLIC_PROJECTION_MAX_ENTRIES = 16
_FINAL_ANSWER_PUBLIC_PROJECTION_MAX_FIELD_ENTRIES = 32
_FINAL_ANSWER_PUBLIC_PROJECTION_MAX_IDENTIFIERS = 32
_LATE_EFFECT_OBSERVATION_MAX_ID_CHARS = 512
_LATE_EFFECT_APPEND_WORKERS = 2
_LATE_EFFECT_APPEND_MAX_PENDING = 32
_LATE_EFFECT_APPEND_EXECUTOR = ThreadPoolExecutor(
    max_workers=_LATE_EFFECT_APPEND_WORKERS,
    thread_name_prefix="late-effect-observation",
)
_LATE_EFFECT_APPEND_SLOTS = threading.BoundedSemaphore(
    _LATE_EFFECT_APPEND_WORKERS + _LATE_EFFECT_APPEND_MAX_PENDING
)
_FINAL_ANSWER_PROJECTION_SECRET_KEY_PARTS = (
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "encrypted_content",
    "oauth",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)

_TOOL_CALLING_SELECTOR_VERDICTS = {"tool_seeking", "tool_calling"}
_PLAIN_RESPONSE_WORKFLOW_IDS = {
    "#V#chat_assistant_workflow",
    "#V#plain_response_workflow",
}
_CONCEPT_PROFILE_RETRIEVAL_WORKFLOW_IDS = {
    "#V#concept_search_instance_retrieval_workflow",
}
_ABSTAIN_OR_NO_SAFE_ROUTE_SELECTOR_VERDICTS = {
    "abstain",
    "escalate",
    "escalation_required",
    "no_safe_route",
    "no_safe_route_available",
    "no_safe_route_found",
}
_FALSE_SUCCESS_FAILURE_MODES = {
    "false_completion_claim",
    "false_completion_gate_state",
}
_EXECUTION_CORRECTNESS_METRIC_LABEL_NAMES = (
    "successful_completion",
    "false_success",
    "unresolved_follow_up_needed",
    "tool_or_workflow_misrouting",
    "abstain_escalate_no_safe_route",
    "workflow_discovery_timeout",
    "required_evidence_missing",
)

_SELECTOR_PROMPT_FAILURE_VERDICTS = {
    "selector_exception",
    "selector_prompt_missing_candidate_list",
    "selector_prompt_missing_turn_text",
    "selector_prompt_render_error",
    "selector_prompt_unavailable",
}

_TOOL_EXECUTION_FAILURE_REASON_MAP = {
    "worker_unavailable_zero_execution": "Tool execution was blocked while workers were unavailable.",
    "missing_tool_call_parse_error": "Tool call parsing failed before any tool execution occurred.",
    "missing_tool_call_retry_exhausted": "Tool-call recovery exhausted retries without executing a tool.",
    "missing_tool_call_unresolved": "Tool-calling was selected but no executable tool call was produced.",
    "tool_dispatch_boundary_missing": "Tool-calling workflow selected but no dispatch boundary evidence was recorded.",
    "tool_dispatch_contract_unresolved": "Tool-calling workflow selected but its dispatch contract was unresolved.",
    "tool_dispatch_handoff_zero_execution": "Tool-calling workflow handoff started but no tool execution was observed.",
    "tool_dispatch_not_started": "Tool-calling workflow selected but the tool dispatch handoff did not start.",
    "tool_dispatch_workflow_missing": "Tool-calling workflow selected but the dispatch workflow was missing.",
    "tool_pipeline_setup_exception": "Tool-pipeline setup failed before the first action could start.",
    "tool_pipeline_execution_exception": "Tool-pipeline execution failed before the first tool action completed.",
}
_CUSTOM_WORKFLOW_EXECUTION_FAILURE_REASON = (
    "Selected workflow execution did not complete successfully."
)

_REPRESENTATION_CONTRACT_SCHEMA_VERSION = "required_effects_contract.v1"
_AUX_REQUIRED_SCHOLARLY_REPRESENTATION_FILE_COPY_ID_FIELDS = (
    "required_scholarly_representation_for_file_copy_ids",
)
_AUX_REQUIRED_REPRESENTATION_FILE_COPY_ID_FIELDS = (
    "required_representation_for_file_copy_ids",
)
_AUX_REQUIRED_TOOL_FIELDS = (
    "required_tools",
    "explicit_required_tools",
    "contract_required_tools",
)

_REPRESENTATION_CONFIG_UNAVAILABLE_FAILURE_CODE = (
    "representation_profile_catalogue_unavailable"
)
_REPRESENTATION_PROFILE_UNMATCHED_FAILURE_CODE = "representation_profile_unmatched"
_REPRESENTATION_FAILURE_REASON_MAP = {
    _REPRESENTATION_CONFIG_UNAVAILABLE_FAILURE_CODE: (
        "Required representation profile catalogue is unavailable."
    ),
    _REPRESENTATION_PROFILE_UNMATCHED_FAILURE_CODE: (
        "No representation profile matched the requested intent."
    ),
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime | None = None) -> str:
    dt = value or _now_utc()
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_write_request_evidence_map(
    raw_value: Any,
) -> dict[str, dict[str, Any]]:
    normalised: dict[str, dict[str, Any]] = {}
    if isinstance(raw_value, Mapping):
        iterable = raw_value.items()
    elif isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        iterable = [
            (_safe_str(item.get("tool_name")), item)
            for item in raw_value
            if isinstance(item, Mapping)
        ]
    else:
        return normalised

    for raw_tool_name, raw_entry in iterable:
        tool_name = _safe_str(raw_tool_name)
        if not tool_name or not isinstance(raw_entry, Mapping):
            continue
        normalised[tool_name.lower()] = {
            "request_state": _safe_str(raw_entry.get("request_state"))
            or "low_confidence",
            "confirmation_state": _safe_str(raw_entry.get("confirmation_state"))
            or "low_confidence",
            "denial_state": _safe_str(raw_entry.get("denial_state"))
            or "low_confidence",
            "rationale": _safe_str(raw_entry.get("rationale")),
        }
    return normalised


def _extract_write_request_evidence_from_aux(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(aux_llm_calls, Sequence) or isinstance(
        aux_llm_calls, (str, bytes)
    ):
        return {}

    merged: dict[str, dict[str, Any]] = {}
    for entry in reversed(aux_llm_calls):
        if not isinstance(entry, Mapping):
            continue
        if _safe_str(entry.get("type")) != "write_tool_request_evidence":
            continue
        normalised = _normalise_write_request_evidence_map(
            entry.get("request_evidence") or entry.get("tool_evidence")
        )
        for tool_name, evidence in normalised.items():
            merged.setdefault(tool_name, evidence)
    return merged


def _write_request_evidence_denies_tool(
    write_request_evidence: Mapping[str, Mapping[str, Any]] | None,
    tool_name: str,
    *,
    allow_recent_context: bool = True,
) -> bool:
    if not isinstance(write_request_evidence, Mapping):
        return False
    evidence = write_request_evidence.get(str(tool_name or "").strip().lower())
    if not isinstance(evidence, Mapping):
        return False
    denial_state = _safe_str(evidence.get("denial_state")) or "low_confidence"
    if denial_state == "explicit_denial":
        return True
    return allow_recent_context and denial_state == "recent_denial_context"


def _safe_non_negative_int(value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, parsed)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _normalise_failure_codes(raw_codes: Any) -> list[str]:
    if not isinstance(raw_codes, list):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw_codes:
        code = _safe_str(item)
        if not code:
            continue
        lowered = code.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(code)
    return ordered


_CRITICAL_WORKFLOW_FAILURE_CODES: tuple[str, ...] = (
    "workflow_execution_failed",
    "workflow_llm_step_timeout",
    "workflow_llm_step_failed",
    "model_readiness_wait_budget_exhausted",
    "ollama_request_timeout",
    "subworkflow_failed",
)


def _extract_critical_workflow_failure_evidence(
    payload: Mapping[str, Any] | None,
) -> tuple[list[str], str | None]:
    """Extract typed terminal failures from bounded workflow evidence surfaces."""

    if not isinstance(payload, Mapping):
        return [], None
    queue: list[tuple[Mapping[str, Any], int]] = [(payload, 0)]
    seen: set[int] = set()
    codes: list[str] = []
    detail: str | None = None
    text_keys = (
        "error",
        "error_code",
        "failure_code",
        "failure_reason",
        "failure_detail",
        "last_action_error",
        "action_error",
        "subworkflow_error",
        "child_error",
        "timeout_detail",
    )
    while queue and len(seen) < 250:
        surface, depth = queue.pop(0)
        identity = id(surface)
        if identity in seen:
            continue
        seen.add(identity)
        surface_status = (_safe_str(surface.get("status")) or "").lower()
        surface_outcome = (_safe_str(surface.get("action_outcome")) or "").lower()
        terminal_failure_surface = bool(
            surface.get("last_action_failed") is True
            or surface.get("completed") is False
            or surface.get("child_completed") is False
            or surface.get("effective_completed") is False
            or surface_outcome in {"failure", "failed", "error"}
            or surface_status in {"failure", "failed", "timed_out", "error"}
            or (_safe_str(surface.get("dispatch_terminal_status")) or "").lower()
            in {"failure", "failed", "error"}
            or (depth == 0 and _safe_str(surface.get("error")))
        )
        if terminal_failure_surface:
            termination_reason = surface.get("termination_reason")
            if isinstance(termination_reason, Mapping):
                termination_code = _safe_str(termination_reason.get("code"))
                termination_detail = _safe_str(termination_reason.get("detail"))
                if termination_code or termination_detail:
                    if "workflow_execution_failed" not in codes:
                        codes.append("workflow_execution_failed")
                    if detail is None:
                        detail = ": ".join(
                            value
                            for value in (termination_code, termination_detail)
                            if value
                        )
            for key in text_keys:
                text_value = _safe_str(surface.get(key))
                if not text_value:
                    continue
                lowered = text_value.lower()
                matched = False
                for code in _CRITICAL_WORKFLOW_FAILURE_CODES:
                    if code in lowered and code not in codes:
                        codes.append(code)
                        matched = True
                if matched and detail is None:
                    detail = text_value
            raw_codes = surface.get("failure_codes")
            if isinstance(raw_codes, list):
                for raw_code in raw_codes:
                    code_value = (_safe_str(raw_code) or "").lower()
                    for code in _CRITICAL_WORKFLOW_FAILURE_CODES:
                        if code_value == code and code not in codes:
                            codes.append(code)
        if depth >= 5:
            continue
        for key, value in surface.items():
            if key in {
                "prompt",
                "prompt_text",
                "response",
                "response_text",
                # Candidate/provider attempts can fail before represented
                # fallback succeeds. They are duration evidence, not terminal
                # turn evidence by themselves.
                "aux_llm_calls",
                "llm_calls",
            }:
                continue
            if isinstance(value, Mapping):
                queue.append((value, depth + 1))
            elif isinstance(value, list):
                for item in value[-50:]:
                    if isinstance(item, Mapping):
                        queue.append((item, depth + 1))
    return codes, detail


def _custom_workflow_execution_progress_observed(
    custom_workflow_execution: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(custom_workflow_execution, Mapping):
        return False
    if any(
        _safe_non_negative_int(custom_workflow_execution.get(field_name)) > 0
        for field_name in (
            "action_started_count",
            "action_completed_count",
            "action_failure_count",
            "durable_side_effect_count",
            "terminal_effect_count",
        )
    ):
        return True
    observed = custom_workflow_execution.get("observed") is True
    if not observed:
        return False
    return bool(
        _safe_str(custom_workflow_execution.get("terminal_status"))
        or _safe_str(custom_workflow_execution.get("final_state"))
        or _safe_str(custom_workflow_execution.get("error"))
        or isinstance(custom_workflow_execution.get("completed"), bool)
    )


def _derive_execution_signal_completion_blocker(
    *,
    execution_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Fail closed when execution evidence contradicts a completed verdict.

    This helper is only consulted from ``_derive_completion_gate`` after the
    gate would otherwise conclude ``completed`` from required effects and
    postcondition checks alone. It provides the final execution-signal guard
    against false success.

    Return contract:
    - ``None`` means execution evidence does not block a completed verdict.
    - a mapping means execution evidence overrides ``completed`` and supplies a
      synthetic unresolved precondition plus completion-gate decision inputs
      such as ``decision``, ``decision_reason``, ``failure_codes``, and
      ``repeat_eligible``.

    Main branch classes:
    - contracted workflow terminal-success contract missing terminal status
    - contracted workflow terminal-success contract evaluated false
    - terminal dispatch failure for workflow or tool execution
    - planned tool execution with zero successful results

    ``repeat_eligible`` is part of the blocker contract, not a UI hint. When it
    is absent downstream consumers must treat the blocker as non-repeatable by
    default.
    """
    if not isinstance(execution_summary, Mapping):
        return None

    selected_execution_mode = (
        _safe_str(execution_summary.get("selected_execution_mode")) or ""
    ).lower()
    dispatch_terminal_status = (
        _safe_str(execution_summary.get("dispatch_terminal_status")) or ""
    ).lower()
    dispatch_terminal_failure_reason = _safe_str(
        execution_summary.get("dispatch_terminal_failure_reason")
    )
    dispatch_terminal_failure_detail = _safe_str(
        execution_summary.get("dispatch_terminal_failure_detail")
    )
    dispatch_workflow_id = _safe_str(execution_summary.get("dispatch_workflow_id"))
    dispatch_event_count = _safe_non_negative_int(
        execution_summary.get("dispatch_event_count")
    )
    planned_count = _safe_non_negative_int(execution_summary.get("planned_count"))
    executed_count = _safe_non_negative_int(execution_summary.get("executed_count"))
    successful_invocation_count = _safe_non_negative_int(
        execution_summary.get("successful_invocation_count")
    )
    failed_invocation_count = _safe_non_negative_int(
        execution_summary.get("failed_invocation_count")
    )
    blocked_invocation_count = _safe_non_negative_int(
        execution_summary.get("blocked_invocation_count")
    )
    zero_tool_reason_code = _safe_str(execution_summary.get("zero_tool_reason_code"))
    failure_codes = _normalise_failure_codes(execution_summary.get("failure_codes"))
    normalised_dispatch_terminal_failure_reason = (
        dispatch_terminal_failure_reason.lower()
        if dispatch_terminal_failure_reason
        else ""
    )
    if normalised_dispatch_terminal_failure_reason and (
        normalised_dispatch_terminal_failure_reason
        not in {code.lower() for code in failure_codes}
    ):
        failure_codes.insert(0, normalised_dispatch_terminal_failure_reason)

    if any(code.lower() == "workflow_llm_step_timeout" for code in failure_codes):
        decision_reason = (
            dispatch_terminal_failure_detail
            or "A workflow LLM stage timed out before execution evidence could be verified."
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
            "source": "execution_signals",
            "workflow_id": dispatch_workflow_id or None,
        }

    critical_workflow_failure_codes = [
        code
        for code in failure_codes
        if code.lower() in set(_CRITICAL_WORKFLOW_FAILURE_CODES)
    ]
    if critical_workflow_failure_codes:
        decision_reason = (
            dispatch_terminal_failure_detail
            or "A critical workflow stage failed before completion could be verified."
        )
        return {
            "effect_id": "effect_critical_workflow_failure_1",
            "effect_type": "workflow_execution",
            "status": "not_executed",
            "status_reason": decision_reason,
            "failure_code": critical_workflow_failure_codes[0],
            "failure_codes": list(critical_workflow_failure_codes),
            "decision": "escalation_required",
            "decision_reason": decision_reason,
            "repeat_eligible": False,
            "source": "execution_signals",
            "workflow_id": dispatch_workflow_id or None,
        }

    effect_type = (
        "workflow_execution"
        if selected_execution_mode == "custom_workflow"
        else "tool_execution"
    )
    custom_workflow_execution = execution_summary.get("custom_workflow_execution")
    custom_workflow_execution_observed = bool(
        isinstance(custom_workflow_execution, Mapping)
        and custom_workflow_execution.get("observed") is True
    )
    terminal_success_evaluation = (
        custom_workflow_execution.get("terminal_success_evaluation")
        if isinstance(custom_workflow_execution, Mapping)
        else None
    )
    terminal_success_contract = (
        custom_workflow_execution.get("terminal_success_contract")
        if isinstance(custom_workflow_execution, Mapping)
        else None
    )
    execution_progress_observed = executed_count > 0
    if effect_type == "workflow_execution":
        execution_progress_observed = execution_progress_observed or (
            _custom_workflow_execution_progress_observed(custom_workflow_execution)
        )

    if (
        effect_type == "workflow_execution"
        and dispatch_event_count <= 0
        and not custom_workflow_execution_observed
        and not dispatch_terminal_status
    ):
        if not failure_codes:
            failure_codes.append("custom_workflow_dispatch_not_started")
        decision_reason = (
            f"{dispatch_workflow_id or 'Selected workflow'} was selected but dispatch "
            "never started."
        )
        return {
            "effect_id": "effect_execution_signal_1",
            "effect_type": effect_type,
            "status": "not_executed",
            "status_reason": decision_reason,
            "failure_code": failure_codes[0],
            "failure_codes": list(failure_codes),
            "decision": "escalation_required",
            "decision_reason": decision_reason,
            "repeat_eligible": False,
            "source": "execution_signals",
            "workflow_id": dispatch_workflow_id or None,
        }

    if (
        effect_type == "workflow_execution"
        and isinstance(terminal_success_contract, Mapping)
        and not isinstance(terminal_success_evaluation, Mapping)
    ):
        terminal_status = _safe_str(
            custom_workflow_execution.get("terminal_status")
            if isinstance(custom_workflow_execution, Mapping)
            else None
        )
        if not terminal_status:
            decision_reason = "Contracted workflow did not report a terminal status."
            return {
                "effect_id": "effect_workflow_terminal_contract_1",
                "effect_type": effect_type,
                "status": "not_satisfied",
                "status_reason": decision_reason,
                "failure_code": "contracted_workflow_terminal_status_missing",
                "failure_codes": ["contracted_workflow_terminal_status_missing"],
                "decision": "failed",
                "decision_reason": decision_reason,
                "repeat_eligible": False,
                "source": "workflow_terminal_success_contract",
                "workflow_id": dispatch_workflow_id or None,
            }

    if effect_type == "workflow_execution" and isinstance(
        terminal_success_evaluation, Mapping
    ):
        contract_succeeded = terminal_success_evaluation.get("success")
        if contract_succeeded is False:
            contract_failure_codes = _normalise_failure_codes(
                terminal_success_evaluation.get("failure_codes")
            )
            if not contract_failure_codes:
                contract_failure_codes = [
                    "contracted_workflow_terminal_success_contract_unmet"
                ]
            decision_reason = (
                _safe_str(terminal_success_evaluation.get("decision_reason"))
                or "Contracted workflow terminal-success requirements were not met."
            )
            return {
                "effect_id": "effect_workflow_terminal_contract_1",
                "effect_type": effect_type,
                "status": "not_satisfied",
                "status_reason": decision_reason,
                "failure_code": contract_failure_codes[0],
                "failure_codes": list(contract_failure_codes),
                "decision": "failed",
                "decision_reason": decision_reason,
                "repeat_eligible": False,
                "source": "workflow_terminal_success_contract",
                "workflow_id": dispatch_workflow_id or None,
            }

    if dispatch_terminal_status == "failed":
        if not failure_codes:
            failure_codes.append(
                "custom_workflow_dispatch_failed"
                if effect_type == "workflow_execution"
                else "tool_dispatch_failed"
            )
        if dispatch_terminal_failure_detail:
            decision_reason = dispatch_terminal_failure_detail
        elif effect_type == "workflow_execution":
            decision_reason = (
                _infer_custom_workflow_required_effect(
                    execution_summary=execution_summary
                )
                or {}
            ).get("status_reason") or _CUSTOM_WORKFLOW_EXECUTION_FAILURE_REASON
        else:
            decision_reason = _TOOL_EXECUTION_FAILURE_REASON_MAP.get(
                failure_codes[0],
                "Planned tool execution did not complete successfully.",
            )
        workflow_contract_present = isinstance(terminal_success_contract, Mapping) or (
            isinstance(terminal_success_evaluation, Mapping)
        )
        if effect_type == "workflow_execution" and workflow_contract_present:
            status = "not_satisfied"
        else:
            status = "not_satisfied" if execution_progress_observed else "not_executed"
        return {
            "effect_id": "effect_execution_signal_1",
            "effect_type": effect_type,
            "status": status,
            "status_reason": decision_reason,
            "failure_code": failure_codes[0],
            "failure_codes": list(failure_codes),
            "decision": (
                "failed" if status == "not_satisfied" else "escalation_required"
            ),
            "decision_reason": decision_reason,
            "repeat_eligible": False,
            "source": "execution_signals",
            "workflow_id": dispatch_workflow_id or None,
        }

    if planned_count <= 0 or successful_invocation_count > 0:
        return None

    if not failure_codes:
        failure_codes.append(
            zero_tool_reason_code or "planned_tool_execution_without_success"
        )

    status = (
        "not_satisfied"
        if (
            executed_count > 0
            or failed_invocation_count > 0
            or blocked_invocation_count > 0
        )
        else "not_executed"
    )
    if status == "not_satisfied":
        decision_reason = (
            "Planned tool execution completed without any successful tool result."
        )
    else:
        decision_reason = _TOOL_EXECUTION_FAILURE_REASON_MAP.get(
            failure_codes[0],
            "Planned tool execution did not produce any successful tool result.",
        )

    return {
        "effect_id": "effect_execution_signal_1",
        "effect_type": "tool_execution",
        "status": status,
        "status_reason": decision_reason,
        "failure_code": failure_codes[0],
        "failure_codes": list(failure_codes),
        "decision": "failed" if status == "not_satisfied" else "escalation_required",
        "decision_reason": decision_reason,
        "repeat_eligible": status != "not_satisfied",
        "source": "execution_signals",
    }


def _classify_turn_execution_failure_mode(item: Mapping[str, Any]) -> str:
    """Classify a turn execution record into a detailed failure mode."""

    decision = (_safe_str(item.get("decision")) or "").lower()
    unresolved_effect_count = _safe_non_negative_int(
        item.get("unresolved_effect_count"),
        default=0,
    )
    safe_to_claim_completion = bool(item.get("safe_to_claim_completion", True))

    critic_summary_raw = item.get("critic_summary")
    critic_summary = (
        critic_summary_raw if isinstance(critic_summary_raw, Mapping) else {}
    )
    not_verified_count = _safe_non_negative_int(
        critic_summary.get("not_verified_count"),
        default=0,
    )
    inconclusive_count = _safe_non_negative_int(
        critic_summary.get("inconclusive_count"),
        default=0,
    )
    error_count = _safe_non_negative_int(
        critic_summary.get("error_count"),
        default=0,
    )

    completion_claim_detected = bool(item.get("completion_claim_detected", False))
    completion_claim_validated = bool(item.get("completion_claim_validated", True))

    workflow_routing_diagnostics_raw = item.get("workflow_routing_diagnostics")
    workflow_routing_diagnostics = (
        workflow_routing_diagnostics_raw
        if isinstance(workflow_routing_diagnostics_raw, Mapping)
        else {}
    )
    dispatch_raw = workflow_routing_diagnostics.get("dispatch")
    dispatch = dispatch_raw if isinstance(dispatch_raw, Mapping) else {}
    execution_signal_blocker = _derive_execution_signal_completion_blocker(
        execution_summary=dispatch
    )
    required_evidence_answer_consistency_blocked = bool(
        item.get("required_evidence_answer_consistency_blocked", False)
    )

    if required_evidence_answer_consistency_blocked:
        return "false_completion_claim"

    if decision == "failed":
        return "mutation_failed_or_blocked"
    if decision == "escalation_required":
        return "mutation_not_executed"
    if decision == "partial":
        if unresolved_effect_count > 0:
            return "unresolved_required_effects"
        if (not_verified_count + inconclusive_count + error_count) > 0:
            return "postcondition_inconclusive"
        return "partial_unspecified"
    if decision == "completed":
        if execution_signal_blocker is not None:
            return "false_completion_gate_state"
        if not safe_to_claim_completion:
            return "false_completion_gate_state"
        if unresolved_effect_count > 0:
            return "false_completion_claim"
        if (not_verified_count + inconclusive_count + error_count) > 0:
            return "false_completion_claim"
        if completion_claim_detected and not completion_claim_validated:
            return "unvalidated_completion_claim"
        return "completed_verified"
    if completion_claim_detected and not completion_claim_validated:
        return "unvalidated_completion_claim"
    return "unknown"


def is_turn_execution_likely_failure_to_act(failure_mode: str) -> bool:
    return failure_mode in {
        "mutation_failed_or_blocked",
        "mutation_not_executed",
        "unresolved_required_effects",
        "postcondition_inconclusive",
        "false_completion_gate_state",
        "false_completion_claim",
        "unvalidated_completion_claim",
        "partial_unspecified",
    }


def _has_tool_route_launchability_degradation(
    workflow_routing_diagnostics: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(workflow_routing_diagnostics, Mapping):
        return False

    selector_payload = workflow_routing_diagnostics.get("selector")
    selector_payload = selector_payload if isinstance(selector_payload, Mapping) else {}
    override_events = selector_payload.get("override_events")
    if not isinstance(override_events, list):
        return False

    for event in override_events:
        if not isinstance(event, Mapping):
            continue
        prior_selected_workflow_id = _safe_str(event.get("prior_selected_workflow_id"))
        selected_workflow_id = _safe_str(event.get("selected_workflow_id"))
        reason = (_safe_str(event.get("reason")) or "").lower()
        custom_override_reason = (
            _safe_str(event.get("custom_workflow_override_reason")) or ""
        ).lower()
        launch_viability_probe = event.get("launch_viability_probe")
        prior_probe = (
            launch_viability_probe.get("prior_selected_workflow")
            if isinstance(launch_viability_probe, Mapping)
            else None
        )
        prior_launchable = (
            prior_probe.get("launchable") if isinstance(prior_probe, Mapping) else None
        )

        if not prior_selected_workflow_id:
            continue
        if selected_workflow_id != "#V#tool_calling_workflow":
            continue
        if (
            reason
            == "selected_custom_workflow_launchability_requires_safe_general_fallback"
        ):
            return True
        if "launchability" in reason:
            return True
        if custom_override_reason and prior_launchable is False:
            return True

    return False


def build_turn_execution_correctness_summary(
    *,
    completion_gate: Mapping[str, Any] | None,
    required_effects: Sequence[Mapping[str, Any]] | None,
    critic_summary: Mapping[str, Any] | None,
    final_response: Mapping[str, Any] | None,
    workflow_selection: Mapping[str, Any] | None,
    workflow_routing_diagnostics: Mapping[str, Any] | None,
) -> dict[str, Any]:
    required_effect_list = [
        dict(effect)
        for effect in (required_effects or ())
        if isinstance(effect, Mapping)
    ]
    unresolved_effect_count = 0
    for effect in required_effect_list:
        status = (_safe_str(effect.get("status")) or "").lower()
        if status in {"not_executed", "not_satisfied"}:
            unresolved_effect_count += 1

    critic_summary_payload = (
        dict(critic_summary) if isinstance(critic_summary, Mapping) else {}
    )
    workflow_selection_payload = (
        dict(workflow_selection) if isinstance(workflow_selection, Mapping) else {}
    )
    workflow_routing_payload = (
        dict(workflow_routing_diagnostics)
        if isinstance(workflow_routing_diagnostics, Mapping)
        else {}
    )
    dispatch_raw = workflow_routing_payload.get("dispatch")
    dispatch = dispatch_raw if isinstance(dispatch_raw, Mapping) else {}
    discovery_raw = workflow_routing_payload.get("discovery")
    discovery = discovery_raw if isinstance(discovery_raw, Mapping) else {}
    completion_gate_payload = (
        dict(completion_gate) if isinstance(completion_gate, Mapping) else {}
    )
    final_response_payload = (
        dict(final_response) if isinstance(final_response, Mapping) else {}
    )

    selected_workflow_id = _safe_str(
        workflow_selection_payload.get("selected_workflow_id")
    )
    selector_verdict = _safe_str(workflow_selection_payload.get("selector_verdict"))
    selector_verdict_lower = (selector_verdict or "").lower()
    selector_source = _safe_str(workflow_selection_payload.get("selector_source"))
    plain_response_route_selected = (
        selected_workflow_id or ""
    ) in _PLAIN_RESPONSE_WORKFLOW_IDS or selector_verdict_lower == "plain_response"
    tool_route_selected = (
        (selected_workflow_id or "") == "#V#tool_calling_workflow"
        or selector_verdict_lower in _TOOL_CALLING_SELECTOR_VERDICTS
    )
    custom_workflow_route_selected = bool(
        selected_workflow_id
        and not tool_route_selected
        and not plain_response_route_selected
    )
    dispatch = dict(dispatch)
    if not _safe_str(dispatch.get("selected_execution_mode")):
        if tool_route_selected:
            dispatch["selected_execution_mode"] = "tool_pipeline"
        elif custom_workflow_route_selected:
            dispatch["selected_execution_mode"] = "custom_workflow"
        elif plain_response_route_selected:
            dispatch["selected_execution_mode"] = "direct_response"
    if (
        not _safe_str(dispatch.get("dispatch_workflow_id"))
        and selected_workflow_id
        and (custom_workflow_route_selected or plain_response_route_selected)
    ):
        dispatch["dispatch_workflow_id"] = selected_workflow_id
    workflow_routing_payload["dispatch"] = dict(dispatch)

    decision = _safe_str(completion_gate_payload.get("decision"))
    requires_follow_up = bool(completion_gate_payload.get("requires_follow_up", False))
    safe_to_claim_completion = bool(
        completion_gate_payload.get("safe_to_claim_completion", not requires_follow_up)
    )
    required_evidence_answer_consistency_blocked = (
        _completion_gate_has_required_evidence_answer_consistency_blocker(
            completion_gate_payload
        )
    )

    failure_mode = _classify_turn_execution_failure_mode(
        {
            "decision": decision,
            "unresolved_effect_count": unresolved_effect_count,
            "safe_to_claim_completion": safe_to_claim_completion,
            "critic_summary": critic_summary_payload,
            "completion_claim_detected": final_response_payload.get(
                "completion_claim_detected"
            ),
            "completion_claim_validated": final_response_payload.get(
                "completion_claim_validated"
            ),
            "workflow_routing_diagnostics": workflow_routing_payload,
            "required_evidence_answer_consistency_blocked": (
                required_evidence_answer_consistency_blocked
            ),
        }
    )
    likely_failure_to_act = is_turn_execution_likely_failure_to_act(failure_mode)
    launchability_degraded_tool_route = bool(
        tool_route_selected
        and _has_tool_route_launchability_degradation(workflow_routing_payload)
    )
    abstain_escalate_no_safe_route = (
        selector_verdict_lower in _ABSTAIN_OR_NO_SAFE_ROUTE_SELECTOR_VERDICTS
        or (
            not selected_workflow_id
            and (decision or "").lower() == "escalation_required"
            and unresolved_effect_count == 0
        )
    )
    tool_or_workflow_misrouting = bool(
        likely_failure_to_act
        and (plain_response_route_selected or launchability_degraded_tool_route)
        and not abstain_escalate_no_safe_route
    )
    false_success = failure_mode in _FALSE_SUCCESS_FAILURE_MODES
    successful_completion = failure_mode == "completed_verified"
    workflow_discovery_timeout = bool(discovery.get("budget_exhausted")) or (
        (_safe_str(discovery.get("match_absence_reason")) or "").lower()
        == "workflow_discovery_budget_exhausted"
    )
    required_evidence_missing = bool(
        unresolved_effect_count > 0
        and any(
            (_safe_str(effect.get("effect_type")) or "").lower()
            in {"grounded_evidence", "prompt_required_evidence"}
            for effect in required_effect_list
        )
    )

    metric_labels = {
        "successful_completion": successful_completion,
        "false_success": false_success,
        "unresolved_follow_up_needed": requires_follow_up,
        "tool_or_workflow_misrouting": tool_or_workflow_misrouting,
        "abstain_escalate_no_safe_route": abstain_escalate_no_safe_route,
        "workflow_discovery_timeout": workflow_discovery_timeout,
        "required_evidence_missing": required_evidence_missing,
    }

    overall_outcome = "unknown"
    if metric_labels["false_success"]:
        overall_outcome = "false_success"
    elif metric_labels["tool_or_workflow_misrouting"]:
        overall_outcome = "tool_or_workflow_misrouting"
    elif metric_labels["abstain_escalate_no_safe_route"]:
        overall_outcome = "abstain_escalate_no_safe_route"
    elif failure_mode == "mutation_failed_or_blocked":
        overall_outcome = "mutation_failed_or_blocked"
    elif metric_labels["unresolved_follow_up_needed"]:
        overall_outcome = "unresolved_follow_up_needed"
    elif metric_labels["successful_completion"]:
        overall_outcome = "successful_completion"

    return {
        "schema_version": TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION,
        "overall_outcome": overall_outcome,
        "failure_mode": failure_mode,
        "likely_failure_to_act": likely_failure_to_act,
        "metric_labels": metric_labels,
        "selection_labels": {
            "selected_workflow_id": selected_workflow_id,
            "selector_verdict": selector_verdict,
            "selector_source": selector_source,
            "plain_response_route_selected": plain_response_route_selected,
            "tool_route_selected": tool_route_selected,
            "launchability_degraded_tool_route": launchability_degraded_tool_route,
            "tool_or_workflow_misrouting": tool_or_workflow_misrouting,
            "abstain_escalate_no_safe_route": abstain_escalate_no_safe_route,
        },
        "gate_labels": {
            "decision": decision,
            "requires_follow_up": requires_follow_up,
            "safe_to_claim_completion": safe_to_claim_completion,
            "required_evidence_answer_consistency_blocked": (
                required_evidence_answer_consistency_blocked
            ),
            "completion_claim_detected": bool(
                final_response_payload.get("completion_claim_detected", False)
            ),
            "completion_claim_validated": bool(
                final_response_payload.get("completion_claim_validated", True)
            ),
        },
        "evidence_counts": {
            "required_effect_count": len(required_effect_list),
            "unresolved_effect_count": unresolved_effect_count,
            "workflow_discovery_candidate_count": _safe_non_negative_int(
                discovery.get("candidate_count"),
                default=0,
            ),
            "workflow_discovery_match_count": _safe_non_negative_int(
                discovery.get("match_count"),
                default=0,
            ),
            "critic_not_verified_count": _safe_non_negative_int(
                critic_summary_payload.get("not_verified_count"),
                default=0,
            ),
            "critic_inconclusive_count": _safe_non_negative_int(
                critic_summary_payload.get("inconclusive_count"),
                default=0,
            ),
            "critic_error_count": _safe_non_negative_int(
                critic_summary_payload.get("error_count"),
                default=0,
            ),
        },
    }


def ensure_turn_execution_record_execution_correctness(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {}

    payload = dict(record)
    critic_raw = payload.get("critic")
    critic_payload = dict(critic_raw) if isinstance(critic_raw, Mapping) else {}
    critic_summary_raw = critic_payload.get("summary")
    critic_summary = (
        dict(critic_summary_raw) if isinstance(critic_summary_raw, Mapping) else {}
    )

    payload["execution_correctness"] = build_turn_execution_correctness_summary(
        completion_gate=(
            payload.get("completion_gate")
            if isinstance(payload.get("completion_gate"), Mapping)
            else None
        ),
        required_effects=(
            payload.get("required_effects")
            if isinstance(payload.get("required_effects"), list)
            else None
        ),
        critic_summary=critic_summary,
        final_response=(
            payload.get("final_response")
            if isinstance(payload.get("final_response"), Mapping)
            else None
        ),
        workflow_selection=(
            payload.get("workflow_selection")
            if isinstance(payload.get("workflow_selection"), Mapping)
            else None
        ),
        workflow_routing_diagnostics=(
            payload.get("workflow_routing_diagnostics")
            if isinstance(payload.get("workflow_routing_diagnostics"), Mapping)
            else None
        ),
    )
    return payload


def _derive_actor_concept_from_namespace(namespace: Any) -> str | None:
    namespace_value = _safe_str(namespace)
    if not namespace_value or not namespace_value.startswith("#V#"):
        return None
    namespace_body = namespace_value[3:].strip()
    if not namespace_body:
        return None
    user_slug = namespace_body.split("@", 1)[0].strip()
    if not user_slug:
        return None
    return f"#V#{user_slug}"


def _resolve_actor_concept_identity(
    *,
    actor_concept_id: Any,
    user_id: Any,
    namespace: Any,
) -> tuple[str | None, str]:
    explicit_actor = _safe_str(actor_concept_id)
    if explicit_actor and "@" not in explicit_actor:
        return explicit_actor, "actor_concept_id"

    explicit_user = _safe_str(user_id)
    if explicit_user and "@" not in explicit_user:
        return explicit_user, "user_id"

    namespace_actor = _derive_actor_concept_from_namespace(namespace)
    if namespace_actor:
        return namespace_actor, "namespace_user_component"

    return None, "missing"


def _normalise_iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return _iso_utc(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return _iso_utc()


def _hash_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_payload(value: Any) -> str | None:
    if value is None:
        return None
    try:
        payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _payload_char_count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        payload = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        try:
            payload = str(value)
        except Exception:
            return None
    return len(payload)


def _payload_preview(
    value: Any, *, max_chars: int = _SEARCH_EVIDENCE_PREVIEW_CHARS
) -> str | None:
    if value is None:
        return None
    try:
        payload = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        try:
            payload = str(value)
        except Exception:
            return None
    if len(payload) <= max_chars:
        return payload
    return payload[:max_chars] + f"\n... [truncated {len(payload) - max_chars} chars]"


def _is_search_evidence_tool(tool_name: str | None) -> bool:
    if not isinstance(tool_name, str):
        return False
    cleaned = tool_name.strip()
    if not cleaned:
        return False
    return is_tool_search_evidence(cleaned)


def _capture_search_evidence_value(
    field_name: str,
    value: Any,
    *,
    max_chars: int,
) -> dict[str, Any]:
    if value is None:
        return {}

    field_payload: dict[str, Any] = {}
    char_count = _payload_char_count(value)
    if char_count is not None:
        field_payload[f"{field_name}_char_count"] = char_count

    fingerprint = _hash_payload(value)
    if fingerprint:
        field_payload[f"{field_name}_sha256"] = fingerprint

    if char_count is None or char_count > max_chars:
        field_payload[f"{field_name}_truncated"] = True
        preview = _payload_preview(value)
        if isinstance(preview, str) and preview:
            field_payload[f"{field_name}_preview"] = preview
        return field_payload

    field_payload[field_name] = value
    field_payload[f"{field_name}_truncated"] = False
    return field_payload


def _build_tool_evidence(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
    *,
    include_search_reads: bool,
    include_verification_reads: bool,
    max_argument_chars: int = _SEARCH_EVIDENCE_MAX_ARGUMENT_CHARS,
    max_result_chars: int = _SEARCH_EVIDENCE_MAX_RESULT_CHARS,
) -> list[dict[str, Any]]:
    """Return authoritative read evidence for later quality inspection.

    Search and verification results are often much more diagnostic than their
    user-facing summaries. Preserve the exact arguments/result when reasonably
    sized and fall back to fingerprint + preview metadata when the payload is
    too large.
    """

    evidence: list[dict[str, Any]] = []

    for invocation in tool_invocations or ():
        if not isinstance(invocation, Mapping):
            continue

        tool_name = _safe_str(invocation.get("tool")) or _safe_str(
            invocation.get("method")
        )
        is_search_read = _is_search_evidence_tool(tool_name)
        is_verification_read = bool(
            tool_name and is_tool_verification_read(tool_name)
        )
        if not (
            (include_search_reads and is_search_read)
            or (include_verification_reads and is_verification_read)
        ):
            continue

        arguments_value = invocation.get("effective_arguments")
        if arguments_value in (None, {}):
            arguments_value = invocation.get("payload")
        if arguments_value in (None, {}):
            arguments_value = invocation.get("arguments")

        result_value = invocation.get("effective_payload")
        if result_value is None:
            result_value = invocation.get("result")

        entry: dict[str, Any] = {
            "tool": tool_name,
            "status": _classify_tool_invocation_status(invocation=invocation),
        }

        error_value = _safe_str(invocation.get("error"))
        if error_value:
            entry["error"] = error_value

        result_summary = _safe_str(invocation.get("result_summary"))
        if not result_summary and isinstance(result_value, Mapping):
            result_summary = _safe_str(result_value.get("result_summary")) or _safe_str(
                result_value.get("summary")
            )
        if result_summary:
            entry["result_summary"] = result_summary

        call_id = _safe_str(invocation.get("call_id"))
        if call_id:
            entry["call_id"] = call_id

        if isinstance(arguments_value, Mapping):
            for key in ("query", "q", "name", "text"):
                query_value = _safe_str(arguments_value.get(key))
                if query_value:
                    entry["query"] = query_value
                    break

        entry.update(
            _capture_search_evidence_value(
                "arguments",
                arguments_value,
                max_chars=max_argument_chars,
            )
        )
        entry.update(
            _capture_search_evidence_value(
                "result",
                result_value,
                max_chars=max_result_chars,
            )
        )
        evidence.append(entry)

    return evidence


def build_search_tool_evidence(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
    *,
    max_argument_chars: int = _SEARCH_EVIDENCE_MAX_ARGUMENT_CHARS,
    max_result_chars: int = _SEARCH_EVIDENCE_MAX_RESULT_CHARS,
) -> list[dict[str, Any]]:
    """Return authoritative search-tool evidence for later quality inspection."""

    return _build_tool_evidence(
        tool_invocations,
        include_search_reads=True,
        include_verification_reads=False,
        max_argument_chars=max_argument_chars,
        max_result_chars=max_result_chars,
    )


def build_verification_tool_evidence(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
    *,
    max_argument_chars: int = _SEARCH_EVIDENCE_MAX_ARGUMENT_CHARS,
    max_result_chars: int = _SEARCH_EVIDENCE_MAX_RESULT_CHARS,
) -> list[dict[str, Any]]:
    """Return content-bearing verification-read evidence for critic inspection."""

    return _build_tool_evidence(
        tool_invocations,
        include_search_reads=False,
        include_verification_reads=True,
        max_argument_chars=max_argument_chars,
        max_result_chars=max_result_chars,
    )


def _extract_required_evidence_answer_consistency_blocker_from_critic_verdict(
    critic_verdict: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(critic_verdict, Mapping):
        return None

    candidate_values: list[Mapping[str, Any]] = []
    direct_candidate = critic_verdict.get(
        "required_evidence_answer_consistency_blocker"
    )
    if isinstance(direct_candidate, Mapping):
        candidate_values.append(direct_candidate)

    evidence_payload_raw = critic_verdict.get("evidence_payload")
    evidence_payload = (
        evidence_payload_raw if isinstance(evidence_payload_raw, Mapping) else {}
    )
    nested_candidate = evidence_payload.get(
        "required_evidence_answer_consistency_blocker"
    )
    if isinstance(nested_candidate, Mapping):
        candidate_values.append(nested_candidate)

    for candidate in candidate_values:
        blocker = {
            str(key): value for key, value in candidate.items() if isinstance(key, str)
        }
        effect_type = _safe_str(blocker.get("effect_type"))
        if effect_type and effect_type != "required_evidence_answer_consistency":
            continue

        blocker.setdefault(
            "effect_id", "effect_prompt_required_evidence_answer_consistency"
        )
        blocker.setdefault("effect_type", "required_evidence_answer_consistency")
        if not _safe_str(blocker.get("status")):
            blocker["status"] = "not_satisfied"
        if not _safe_str(blocker.get("decision")):
            blocker["decision"] = "partial"

        failure_codes = _dedupe_string_sequence(blocker.get("failure_codes") or [])
        failure_code = _safe_str(blocker.get("failure_code"))
        if failure_code and failure_code not in failure_codes:
            failure_codes.append(failure_code)
        if failure_codes:
            blocker["failure_codes"] = failure_codes
            blocker.setdefault("failure_code", failure_codes[0])

        blocker.setdefault("blocker_source", "critic_verdict")
        return blocker
    return None


def _resolve_prompt_required_evidence_answer_consistency_blocker(
    *,
    critic_verdict: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    authoritative_blocker = (
        _extract_required_evidence_answer_consistency_blocker_from_critic_verdict(
            critic_verdict
        )
    )
    if authoritative_blocker is not None:
        blocker_source = _safe_str(authoritative_blocker.get("blocker_source"))
        return authoritative_blocker, blocker_source or "critic_verdict"
    return None, None


def _completion_gate_has_required_evidence_answer_consistency_blocker(
    completion_gate: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(completion_gate, Mapping):
        return False
    evidence_payload_raw = completion_gate.get("evidence_payload")
    evidence_payload = (
        evidence_payload_raw if isinstance(evidence_payload_raw, Mapping) else {}
    )
    blocker = evidence_payload.get("required_evidence_answer_consistency_blocker")
    if isinstance(blocker, Mapping):
        return True
    for code in _dedupe_string_sequence(
        completion_gate.get("blocking_failure_codes") or []
    ):
        lowered = code.lower()
        if lowered.startswith("prompt_required_evidence_") and (
            "contradict" in lowered or "not_safe" in lowered
        ):
            return True
    return False


def _apply_completion_gate_blocker(
    *,
    completion_gate: Mapping[str, Any] | None,
    blocker: Mapping[str, Any],
) -> dict[str, Any]:
    gate_payload = dict(completion_gate) if isinstance(completion_gate, Mapping) else {}
    evidence_payload_raw = gate_payload.get("evidence_payload")
    evidence_payload = (
        dict(evidence_payload_raw) if isinstance(evidence_payload_raw, Mapping) else {}
    )

    unresolved_preconditions: list[dict[str, Any]] = []
    unresolved_raw = evidence_payload.get("unresolved_preconditions")
    if isinstance(unresolved_raw, list):
        unresolved_preconditions.extend(
            dict(item) for item in unresolved_raw if isinstance(item, Mapping)
        )
    unresolved_preconditions.append(
        {
            "effect_id": _safe_str(blocker.get("effect_id"))
            or "effect_required_evidence_answer_consistency_1",
            "effect_type": _safe_str(blocker.get("effect_type"))
            or "required_evidence_answer_consistency",
            "status": _safe_str(blocker.get("status")) or "not_satisfied",
            "status_reason": _safe_str(blocker.get("status_reason")) or "",
            "failure_codes": _dedupe_string_sequence(
                blocker.get("failure_codes") or []
            ),
        }
    )
    evidence_payload["unresolved_preconditions"] = unresolved_preconditions
    evidence_payload["required_evidence_answer_consistency_blocker"] = dict(blocker)
    evidence_payload["completion_outcome"] = "inconclusive"

    blocking_effect_ids = _dedupe_string_sequence(
        [
            *(gate_payload.get("blocking_effect_ids") or []),
            _safe_str(blocker.get("effect_id")),
        ]
    )
    blocking_failure_codes = _dedupe_string_sequence(
        [
            *(gate_payload.get("blocking_failure_codes") or []),
            *(_dedupe_string_sequence(blocker.get("failure_codes") or [])),
            _safe_str(blocker.get("failure_code")),
        ]
    )
    gate_payload.update(
        {
            "decision": _safe_str(blocker.get("decision")) or "partial",
            "decision_reason": _safe_str(blocker.get("decision_reason"))
            or "Answer contradicted retrieved evidence.",
            "blocking_effect_ids": blocking_effect_ids,
            "blocking_failure_codes": blocking_failure_codes,
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
            "repeat_eligible": bool(blocker.get("repeat_eligible", True)),
            "evidence_payload": evidence_payload,
        }
    )
    return gate_payload


def _is_write_tool(tool_name: str | None) -> bool:
    if not isinstance(tool_name, str):
        return False
    cleaned = tool_name.strip()
    if not cleaned:
        return False
    if cleaned.lower().startswith("__"):
        return False
    return is_tool_write(cleaned)


def _is_verification_read_tool(tool_name: str | None) -> bool:
    if not isinstance(tool_name, str):
        return False
    cleaned = tool_name.strip()
    if not cleaned:
        return False
    if cleaned.lower().startswith("__"):
        return False
    return is_tool_verification_read(cleaned)


def _extract_workflow_ids(raw_items: Any) -> list[str]:
    if not isinstance(raw_items, list):
        return []
    collected: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        workflow_id: str | None = None
        if isinstance(item, str):
            workflow_id = _safe_str(item)
        elif isinstance(item, Mapping):
            for key in ("concept_id", "workflow_id", "id"):
                workflow_id = _safe_str(item.get(key))
                if workflow_id:
                    break
        if not workflow_id:
            continue
        lowered = workflow_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        collected.append(workflow_id)
    return collected


def _extract_workflow_discovery(
    workflow_discovery: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    if not isinstance(workflow_discovery, Mapping):
        return {"candidate_ids": [], "excluded_candidate_ids": []}

    candidate_ids = _extract_workflow_ids(workflow_discovery.get("candidate_ids"))
    if not candidate_ids:
        candidate_ids = _extract_workflow_ids(workflow_discovery.get("candidates"))
    if not candidate_ids:
        candidate_ids = _extract_workflow_ids(workflow_discovery.get("matches"))

    excluded_ids = _extract_workflow_ids(
        workflow_discovery.get("excluded_candidate_ids")
    )
    if not excluded_ids:
        excluded_ids = _extract_workflow_ids(workflow_discovery.get("excluded"))
    if not excluded_ids:
        excluded_ids = _extract_workflow_ids(
            workflow_discovery.get("excluded_candidates")
        )

    return {
        "candidate_ids": candidate_ids,
        "excluded_candidate_ids": excluded_ids,
    }


def _collect_aux_entries(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    *,
    entry_type: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    expected_type = entry_type.strip().lower()
    for entry in aux_llm_calls or ():
        if not isinstance(entry, Mapping):
            continue
        candidate_type = (_safe_str(entry.get("type")) or "").lower()
        if candidate_type != expected_type:
            continue
        entries.append(
            {str(key): value for key, value in entry.items() if isinstance(key, str)}
        )
    return entries


def _build_text_capture(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return {
        "text": text,
        "char_count": len(text),
        "sha256": _hash_text(text),
    }


def _count_named_values(
    values: Sequence[Mapping[str, Any]] | None,
    *,
    field_name: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in values or ():
        if not isinstance(entry, Mapping):
            continue
        name = _safe_str(entry.get(field_name))
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts


def _sorted_count_entries(counts: Mapping[str, int] | None) -> list[dict[str, Any]]:
    items = counts.items() if isinstance(counts, Mapping) else ()
    return [
        {"name": name, "count": count}
        for name, count in sorted(
            (
                (str(name), int(count))
                for name, count in items
                if isinstance(name, str) and str(name).strip()
            ),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def _normalise_workflow_execution_terminal_effects(
    raw_effects: Any,
) -> list[dict[str, Any]]:
    if not isinstance(raw_effects, list):
        return []
    normalised: list[dict[str, Any]] = []
    for raw in raw_effects:
        if not isinstance(raw, Mapping):
            continue
        normalised.append(
            {
                "state_id": _safe_str(raw.get("state_id")),
                "symbol": _safe_str(raw.get("symbol")),
                "alias": _safe_str(raw.get("alias")),
                "applied": bool(raw.get("applied")),
            }
        )
    return normalised


def _normalise_workflow_execution_side_effects(
    raw_effects: Any,
) -> list[dict[str, Any]]:
    if not isinstance(raw_effects, list):
        return []
    normalised: list[dict[str, Any]] = []
    for raw in raw_effects:
        if not isinstance(raw, Mapping):
            continue
        artefact_ids = _dedupe_string_sequence(raw.get("artefact_ids") or [])
        normalised.append(
            {
                "mutation_kind": _safe_str(raw.get("mutation_kind")),
                "artefact_type": _safe_str(raw.get("artefact_type")),
                "source_key": _safe_str(raw.get("source_key")),
                "source_path": _safe_str(raw.get("source_path")),
                "artefact_count": (
                    _safe_non_negative_int(raw.get("artefact_count"))
                    or len(artefact_ids)
                ),
                "artefact_ids": artefact_ids,
            }
        )
    return normalised


def _normalise_workflow_execution_action_ids(raw_action_ids: Any) -> list[str]:
    return _dedupe_string_sequence(
        raw_action_ids if isinstance(raw_action_ids, list) else []
    )


def _normalise_workflow_execution_action_observations(
    raw_observations: Any,
    *,
    workflow_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(raw_observations, list):
        return []
    normalised: list[dict[str, Any]] = []
    for raw in raw_observations:
        if not isinstance(raw, Mapping):
            continue
        action_id = _safe_str(raw.get("action_id"))
        if not action_id:
            continue
        observation = {
            "source": "workflow_action_execution",
            "tool": action_id,
            "action_id": action_id,
            "workflow_id": _safe_str(raw.get("workflow_id")) or workflow_id,
            "state_id": _safe_str(raw.get("state_id")),
            "status": _safe_str(raw.get("action_status"))
            or _safe_str(raw.get("outcome")),
            "outcome": _safe_str(raw.get("outcome"))
            or _safe_str(raw.get("action_status")),
        }
        for field_name in _EXECUTION_SURFACE_CONTEXT_FIELDS:
            value = _safe_str(raw.get(field_name))
            if value:
                observation[field_name] = value
        for field_name in ("details", "error_details"):
            value = raw.get(field_name)
            if isinstance(value, Mapping):
                observation[field_name] = dict(value)
        normalised.append(observation)
    return normalised


def _build_custom_workflow_execution_summary(
    *,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    selected_execution_mode: str,
    selected_workflow_id: str,
    dispatch_workflow_id: str,
    dispatch_terminal_completed: bool | None,
    dispatch_terminal_final_state: str,
    dispatch_terminal_failing_state_id: str,
    dispatch_terminal_failing_action_id: str,
    selected_workflow_trace: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    workflow_execution_entries = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_execution",
    )
    if selected_execution_mode != "custom_workflow" and not workflow_execution_entries:
        return None

    selected_entry: Mapping[str, Any] | None = None
    selected_dispatch_workflow_id = _safe_str(dispatch_workflow_id) or ""
    selected_workflow_id_text = _safe_str(selected_workflow_id) or ""
    for candidate in reversed(workflow_execution_entries):
        candidate_workflow_id = _safe_str(candidate.get("workflow_id")) or ""
        if (
            selected_dispatch_workflow_id
            and candidate_workflow_id.lower() == selected_dispatch_workflow_id.lower()
        ):
            selected_entry = candidate
            break
        if (
            selected_workflow_id_text
            and candidate_workflow_id.lower() == selected_workflow_id_text.lower()
        ):
            selected_entry = candidate
            break
    if selected_entry is None and workflow_execution_entries:
        selected_entry = workflow_execution_entries[-1]

    summary_payload = (
        selected_entry.get("execution_summary")
        if isinstance(selected_entry, Mapping)
        else None
    )
    if not isinstance(summary_payload, Mapping):
        summary_payload = {}

    trace_payload = (
        dict(selected_workflow_trace)
        if isinstance(selected_workflow_trace, Mapping)
        else {}
    )
    trace_completed = trace_payload.get("child_workflow_completed")
    if not isinstance(trace_completed, bool):
        trace_completed = None
    trace_final_state = _safe_str(trace_payload.get("child_workflow_final_state")) or ""
    trace_error = _safe_str(trace_payload.get("child_workflow_error")) or ""
    trace_completion_report_source = (
        _safe_str(trace_payload.get("completion_report_source")) or ""
    )
    trace_execution_summary = (
        dict(cast(Mapping[str, Any], trace_payload.get("workflow_execution_summary")))
        if isinstance(trace_payload.get("workflow_execution_summary"), Mapping)
        else {}
    )
    trace_result_snapshot = (
        dict(cast(Mapping[str, Any], trace_payload.get("child_result_snapshot")))
        if isinstance(trace_payload.get("child_result_snapshot"), Mapping)
        else None
    )
    trace_observed = bool(
        isinstance(trace_completed, bool)
        or trace_final_state
        or trace_error
        or trace_completion_report_source
        or trace_result_snapshot
    )
    trace_terminal_status = ""
    if isinstance(trace_completed, bool):
        trace_terminal_status = "completed" if trace_completed else "failed"
    elif trace_error:
        trace_terminal_status = "failed"
    elif trace_final_state:
        trace_terminal_status = (
            "completed" if trace_final_state.lower() == "completed" else "failed"
        )

    summary_source = summary_payload if isinstance(summary_payload, Mapping) else {}
    if not summary_source and trace_execution_summary:
        summary_source = trace_execution_summary

    terminal_effects = _normalise_workflow_execution_terminal_effects(
        summary_source.get("terminal_effects")
    )
    durable_side_effects = _normalise_workflow_execution_side_effects(
        summary_source.get("durable_side_effects")
    )

    step_result_envelope_count = _safe_non_negative_int(
        summary_source.get("step_result_envelope_count")
    )
    action_started_count = _safe_non_negative_int(
        summary_source.get("action_started_count"),
        default=step_result_envelope_count,
    )
    action_completed_count = _safe_non_negative_int(
        summary_source.get("action_completed_count"),
        default=step_result_envelope_count,
    )
    action_success_count = _safe_non_negative_int(
        summary_source.get("action_success_count")
    )
    action_failure_count = _safe_non_negative_int(
        summary_source.get("action_failure_count")
    )
    action_unknown_count = _safe_non_negative_int(
        summary_source.get("action_unknown_count")
    )
    runtime_event_count = _safe_non_negative_int(
        summary_source.get("runtime_event_count")
    )
    terminal_effect_count = _safe_non_negative_int(
        summary_source.get("terminal_effect_count"),
        default=len(terminal_effects),
    )
    durable_side_effect_count = _safe_non_negative_int(
        summary_source.get("durable_side_effect_count"),
        default=sum(
            _safe_non_negative_int(item.get("artefact_count"))
            for item in durable_side_effects
        ),
    )

    completed_value = summary_source.get("completed")
    if not isinstance(completed_value, bool) and isinstance(selected_entry, Mapping):
        entry_completed = selected_entry.get("completed")
        if isinstance(entry_completed, bool):
            completed_value = entry_completed
    if not isinstance(completed_value, bool) and isinstance(trace_completed, bool):
        completed_value = trace_completed
    if not isinstance(completed_value, bool):
        completed_value = (
            dispatch_terminal_completed
            if isinstance(dispatch_terminal_completed, bool)
            else None
        )

    final_state = _safe_str(summary_source.get("final_state"))
    if not final_state and isinstance(selected_entry, Mapping):
        final_state = _safe_str(selected_entry.get("final_state"))
    if not final_state:
        final_state = trace_final_state
    if not final_state:
        final_state = _safe_str(dispatch_terminal_final_state)

    first_failing_state_id = _safe_str(summary_source.get("first_failing_state_id"))
    if not first_failing_state_id:
        first_failing_state_id = _safe_str(dispatch_terminal_failing_state_id)
    first_failing_action_id = _safe_str(summary_source.get("first_failing_action_id"))
    if not first_failing_action_id:
        first_failing_action_id = _safe_str(dispatch_terminal_failing_action_id)

    observed = selected_entry is not None or trace_observed
    workflow_id = _safe_str(summary_source.get("workflow_id"))
    if not workflow_id and isinstance(selected_entry, Mapping):
        workflow_id = _safe_str(selected_entry.get("workflow_id"))
    if not workflow_id:
        workflow_id = _safe_str(trace_payload.get("selected_workflow_id"))
    if not workflow_id:
        workflow_id = _safe_str(dispatch_workflow_id) or _safe_str(selected_workflow_id)
    successful_action_ids = _normalise_workflow_execution_action_ids(
        summary_source.get("successful_action_ids")
    )
    failed_action_ids = _normalise_workflow_execution_action_ids(
        summary_source.get("failed_action_ids")
    )
    action_observations = _normalise_workflow_execution_action_observations(
        summary_source.get("action_observations"),
        workflow_id=workflow_id,
    )

    workflow_instance_evidence = _extract_workflow_instance_evidence(trace_payload)
    if trace_result_snapshot:
        _merge_workflow_instance_evidence(
            workflow_instance_evidence,
            _extract_workflow_instance_evidence(trace_result_snapshot),
        )
    if summary_source:
        _merge_workflow_instance_evidence(
            workflow_instance_evidence,
            _extract_workflow_instance_evidence(summary_source),
        )
    workflow_instance_payload = workflow_instance_evidence.get("workflow_instance")

    return {
        "observed": observed,
        "schema_version": _safe_str(summary_source.get("schema_version"))
        or "workflow_execution_summary.v1",
        "workflow_id": workflow_id,
        "completed": completed_value if isinstance(completed_value, bool) else None,
        "effective_completed": (
            summary_source.get("effective_completed")
            if isinstance(summary_source.get("effective_completed"), bool)
            else None
        ),
        "terminal_status": _safe_str(summary_source.get("terminal_status"))
        or trace_terminal_status
        or None,
        "final_state": final_state,
        "error": trace_error or None,
        "completion_report_source": trace_completion_report_source or None,
        "result_snapshot": trace_result_snapshot,
        "workflow_instance": (
            dict(workflow_instance_payload)
            if isinstance(workflow_instance_payload, Mapping)
            else None
        ),
        "workflow_instance_id": _safe_str(
            workflow_instance_evidence.get("workflow_instance_id")
        )
        or _safe_str(workflow_instance_evidence.get("instance_id")),
        "workflow_instance_status": _safe_str(workflow_instance_evidence.get("status")),
        "workflow_instance_retry_count": (
            _safe_non_negative_int(workflow_instance_evidence.get("retry_count"))
            if workflow_instance_evidence.get("retry_count") is not None
            else None
        ),
        "workflow_instance_max_retries": (
            _safe_non_negative_int(workflow_instance_evidence.get("max_retries"))
            if workflow_instance_evidence.get("max_retries") is not None
            else None
        ),
        "completion_gate_safe_to_claim_completion": (
            summary_source.get("completion_gate_safe_to_claim_completion")
            if isinstance(
                summary_source.get("completion_gate_safe_to_claim_completion"), bool
            )
            else None
        ),
        "completion_gate_blocking_reason_codes": _dedupe_string_sequence(
            summary_source.get("completion_gate_blocking_reason_codes") or []
        ),
        "terminal_success_contract": (
            dict(
                cast(
                    Mapping[str, Any],
                    summary_source.get("terminal_success_contract"),
                )
            )
            if isinstance(summary_source.get("terminal_success_contract"), Mapping)
            else None
        ),
        "terminal_success_evaluation": (
            dict(
                cast(
                    Mapping[str, Any],
                    summary_source.get("terminal_success_evaluation"),
                )
            )
            if isinstance(summary_source.get("terminal_success_evaluation"), Mapping)
            else None
        ),
        "step_result_envelope_count": step_result_envelope_count,
        "action_started_count": action_started_count,
        "action_completed_count": action_completed_count,
        "action_success_count": action_success_count,
        "action_failure_count": action_failure_count,
        "action_unknown_count": action_unknown_count,
        "successful_action_ids": successful_action_ids,
        "failed_action_ids": failed_action_ids,
        "action_observations": action_observations,
        "first_failing_state_id": first_failing_state_id,
        "first_failing_action_id": first_failing_action_id,
        "runtime_event_count": runtime_event_count,
        "terminal_effect_count": terminal_effect_count,
        "terminal_effects": terminal_effects,
        "durable_side_effect_count": durable_side_effect_count,
        "durable_side_effects": durable_side_effects,
    }


def _derive_zero_tool_execution_reason(
    *,
    selected_execution_mode: str,
    tool_route_selected: bool,
    tool_executed_count: int,
    failure_codes: Sequence[str],
    dispatch_terminal_status: str,
    dispatch_terminal_completed: bool | None,
    custom_workflow_execution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if tool_executed_count > 0:
        return {
            "zero_tool_reason_code": None,
            "zero_tool_reason": None,
            "zero_tool_execution_expected": None,
        }

    custom_execution = (
        custom_workflow_execution
        if isinstance(custom_workflow_execution, Mapping)
        else {}
    )
    if tool_route_selected:
        primary_failure_code = _safe_str(failure_codes[0]) if failure_codes else None
        reason_code = primary_failure_code or "tool_dispatch_zero_execution"
        return {
            "zero_tool_reason_code": reason_code,
            "zero_tool_reason": _TOOL_EXECUTION_FAILURE_REASON_MAP.get(
                reason_code,
                "Tool-calling workflow selected but no tool execution was observed.",
            ),
            "zero_tool_execution_expected": False,
        }

    if (selected_execution_mode or "").lower() == "custom_workflow":
        action_completed_count = _safe_non_negative_int(
            custom_execution.get("action_completed_count")
        )
        terminal_effect_count = _safe_non_negative_int(
            custom_execution.get("terminal_effect_count")
        )
        durable_side_effect_count = _safe_non_negative_int(
            custom_execution.get("durable_side_effect_count")
        )
        if (
            action_completed_count > 0
            or terminal_effect_count > 0
            or durable_side_effect_count > 0
        ):
            return {
                "zero_tool_reason_code": "custom_workflow_actions_handled_turn",
                "zero_tool_reason": (
                    "No tools were required because custom-workflow actions handled the turn."
                ),
                "zero_tool_execution_expected": True,
            }
        if (
            isinstance(dispatch_terminal_completed, bool)
            and dispatch_terminal_completed
        ) or (dispatch_terminal_status or "").lower() == "completed":
            return {
                "zero_tool_reason_code": "custom_workflow_completed_without_tool_invocations",
                "zero_tool_reason": (
                    "No tools were required because the custom workflow completed without tool invocations."
                ),
                "zero_tool_execution_expected": True,
            }
        if (dispatch_terminal_status or "").lower() == "failed":
            return {
                "zero_tool_reason_code": "custom_workflow_failed_before_tool_invocation",
                "zero_tool_reason": (
                    "No tools started because the custom workflow failed before any tool invocation."
                ),
                "zero_tool_execution_expected": False,
            }
        return {
            "zero_tool_reason_code": "custom_workflow_telemetry_incomplete",
            "zero_tool_reason": (
                "No tools started and custom-workflow execution evidence was incomplete."
            ),
            "zero_tool_execution_expected": False,
        }

    if (selected_execution_mode or "").lower() == "direct_response":
        return {
            "zero_tool_reason_code": "direct_response_no_tools_required",
            "zero_tool_reason": "No tools were required for this direct response.",
            "zero_tool_execution_expected": True,
        }

    return {
        "zero_tool_reason_code": "no_tool_execution_observed",
        "zero_tool_reason": "No tool execution was observed.",
        "zero_tool_execution_expected": False,
    }


def _custom_workflow_workflow_execute_equivalent_status(
    execution_summary: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(execution_summary, Mapping):
        return None
    if (_safe_str(execution_summary.get("selected_execution_mode")) or "").lower() != (
        "custom_workflow"
    ):
        return None

    custom_workflow_execution = execution_summary.get("custom_workflow_execution")
    custom_workflow_summary: Mapping[str, Any] = (
        custom_workflow_execution
        if isinstance(custom_workflow_execution, Mapping)
        else {}
    )
    dispatch_terminal_status = (
        _safe_str(execution_summary.get("dispatch_terminal_status")) or ""
    ).lower()
    terminal_status = (
        _safe_str(custom_workflow_summary.get("terminal_status")) or ""
    ).lower()
    observed = bool(custom_workflow_summary.get("observed") is True)
    progress_observed = _custom_workflow_execution_progress_observed(
        custom_workflow_summary
    )

    if not (observed or progress_observed or dispatch_terminal_status):
        return None

    terminal_success_evaluation = custom_workflow_summary.get(
        "terminal_success_evaluation"
    )
    if isinstance(terminal_success_evaluation, Mapping):
        terminal_success = terminal_success_evaluation.get("success")
        if terminal_success is False:
            return "failed"
        if terminal_success is True:
            return "satisfied"

    completion_gate_safe = custom_workflow_summary.get(
        "completion_gate_safe_to_claim_completion"
    )
    if completion_gate_safe is False:
        return "failed"
    if completion_gate_safe is True:
        return "satisfied"

    if dispatch_terminal_status == "failed" or terminal_status == "failed":
        return "failed"

    completed_values = (
        custom_workflow_summary.get("effective_completed"),
        custom_workflow_summary.get("completed"),
        execution_summary.get("dispatch_terminal_completed"),
    )
    if any(value is True for value in completed_values):
        return "satisfied"
    if terminal_status == "completed" or dispatch_terminal_status == "completed":
        return "satisfied"
    if any(value is False for value in completed_values) and (
        progress_observed or observed or dispatch_terminal_status
    ):
        return "failed"

    return None


def _merge_workflow_instance_evidence(
    target: dict[str, Any],
    source: Mapping[str, Any] | None,
) -> None:
    if not isinstance(source, Mapping):
        return
    for key in (
        "workflow_instance_id",
        "instance_id",
        "workflow_id",
        "status",
        "retry_count",
        "max_retries",
    ):
        if target.get(key) not in (None, ""):
            continue
        value = source.get(key)
        if value not in (None, ""):
            target[key] = value
    if not isinstance(target.get("workflow_instance"), Mapping):
        workflow_instance = source.get("workflow_instance")
        if isinstance(workflow_instance, Mapping):
            target["workflow_instance"] = dict(workflow_instance)


def _extract_workflow_instance_evidence(
    payload: Mapping[str, Any] | None,
    *,
    depth: int = 0,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or depth > 3:
        return {}

    evidence: dict[str, Any] = {}
    for key in (
        "workflow_instance",
        "selected_workflow_instance",
        "durable_workflow_instance",
    ):
        workflow_instance = payload.get(key)
        if isinstance(workflow_instance, Mapping):
            evidence["workflow_instance"] = dict(workflow_instance)
            _merge_workflow_instance_evidence(evidence, workflow_instance)
            break

    for source_key, target_key in (
        ("workflow_instance_id", "workflow_instance_id"),
        ("selected_workflow_instance_id", "workflow_instance_id"),
        ("instance_id", "instance_id"),
        ("workflow_id", "workflow_id"),
        ("status", "status"),
        ("terminal_status", "status"),
        ("retry_count", "retry_count"),
        ("max_retries", "max_retries"),
    ):
        if evidence.get(target_key) not in (None, ""):
            continue
        value = payload.get(source_key)
        if value not in (None, ""):
            evidence[target_key] = value

    for nested_key in (
        "result_snapshot",
        "child_result_snapshot",
        "workflow_execution_summary",
        "workflow_execution",
        "completion_report",
    ):
        nested = payload.get(nested_key)
        if not isinstance(nested, Mapping):
            continue
        nested_evidence = _extract_workflow_instance_evidence(
            nested,
            depth=depth + 1,
        )
        _merge_workflow_instance_evidence(evidence, nested_evidence)

    return evidence


def _custom_workflow_instance_evidence_observed(
    custom_workflow_summary: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(custom_workflow_summary, Mapping):
        return False
    if isinstance(custom_workflow_summary.get("workflow_instance"), Mapping):
        return True
    return any(
        _safe_str(custom_workflow_summary.get(key))
        for key in ("workflow_instance_id", "instance_id")
    )


def _custom_workflow_workflow_get_instance_equivalent_status(
    execution_summary: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(execution_summary, Mapping):
        return None
    if (_safe_str(execution_summary.get("selected_execution_mode")) or "").lower() != (
        "custom_workflow"
    ):
        return None

    custom_workflow_execution = execution_summary.get("custom_workflow_execution")
    custom_workflow_summary: Mapping[str, Any] = (
        custom_workflow_execution
        if isinstance(custom_workflow_execution, Mapping)
        else {}
    )
    if _custom_workflow_instance_evidence_observed(custom_workflow_summary):
        return "satisfied"
    return None


def _append_tool_name_once(values: list[str], tool_name: str) -> None:
    canonical_key = _tool_requirement_key(tool_name)
    if all(_tool_requirement_key(existing) != canonical_key for existing in values):
        values.append(tool_name)


def _append_execution_surface_observation_once(
    values: list[dict[str, Any]],
    *,
    tool_name: str,
    status: str,
    source: str,
    workflow_id: str = "",
    state_id: str = "",
    action_id: str = "",
    details: Mapping[str, Any] | None = None,
) -> None:
    cleaned_tool_name = _safe_str(tool_name)
    if not cleaned_tool_name:
        return
    cleaned_status = _safe_str(status) or "unknown"
    fingerprint = (
        cleaned_tool_name.lower(),
        cleaned_status.lower(),
        (_safe_str(source) or "execution_surface").lower(),
        (_safe_str(workflow_id) or "").lower(),
        (_safe_str(state_id) or "").lower(),
        (_safe_str(action_id) or cleaned_tool_name).lower(),
    )
    for existing in values:
        existing_fingerprint = (
            (_safe_str(existing.get("tool")) or "").lower(),
            (_safe_str(existing.get("status")) or "").lower(),
            (_safe_str(existing.get("source")) or "").lower(),
            (_safe_str(existing.get("workflow_id")) or "").lower(),
            (_safe_str(existing.get("state_id")) or "").lower(),
            (_safe_str(existing.get("action_id")) or "").lower(),
        )
        if existing_fingerprint == fingerprint:
            return
    observation = {
        "tool": cleaned_tool_name,
        "status": cleaned_status,
        "source": _safe_str(source) or "execution_surface",
        "workflow_id": _safe_str(workflow_id),
        "state_id": _safe_str(state_id),
        "action_id": _safe_str(action_id) or cleaned_tool_name,
    }
    if isinstance(details, Mapping):
        for field_name in _EXECUTION_SURFACE_CONTEXT_FIELDS:
            value = _safe_str(details.get(field_name))
            if value:
                observation[field_name] = value
        for field_name in ("details", "error_details"):
            value = details.get(field_name)
            if isinstance(value, Mapping):
                observation[field_name] = dict(value)
    values.append(observation)


def _augment_tool_outcomes_with_execution_surfaces(
    *,
    successful_tools: Sequence[str],
    failed_tools: Sequence[str],
    execution_summary: Mapping[str, Any] | None,
) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
    augmented_successful_tools = _dedupe_string_sequence(list(successful_tools))
    augmented_failed_tools = _dedupe_string_sequence(list(failed_tools))
    observed_equivalent_tools: list[str] = []
    observed_execution_surfaces: list[dict[str, Any]] = []

    workflow_execute_status = _custom_workflow_workflow_execute_equivalent_status(
        execution_summary
    )
    if workflow_execute_status == "satisfied":
        _append_tool_name_once(augmented_successful_tools, "workflow_execute")
        _append_tool_name_once(observed_equivalent_tools, "workflow_execute")
        _append_execution_surface_observation_once(
            observed_execution_surfaces,
            tool_name="workflow_execute",
            status="success",
            source="custom_workflow_execution",
        )
    elif workflow_execute_status == "failed":
        _append_tool_name_once(augmented_failed_tools, "workflow_execute")
        _append_tool_name_once(observed_equivalent_tools, "workflow_execute")
        _append_execution_surface_observation_once(
            observed_execution_surfaces,
            tool_name="workflow_execute",
            status="failed",
            source="custom_workflow_execution",
        )

    workflow_get_instance_status = (
        _custom_workflow_workflow_get_instance_equivalent_status(execution_summary)
    )
    if workflow_get_instance_status == "satisfied":
        _append_tool_name_once(augmented_successful_tools, "workflow_get_instance")
        _append_tool_name_once(observed_equivalent_tools, "workflow_get_instance")
        _append_execution_surface_observation_once(
            observed_execution_surfaces,
            tool_name="workflow_get_instance",
            status="success",
            source="custom_workflow_execution",
        )

    if isinstance(execution_summary, Mapping):
        custom_workflow_execution = execution_summary.get("custom_workflow_execution")
        if isinstance(custom_workflow_execution, Mapping):
            workflow_id = _safe_str(custom_workflow_execution.get("workflow_id"))
            successful_action_lookup = {
                action_id.lower()
                for action_id in _dedupe_string_sequence(
                    custom_workflow_execution.get("successful_action_ids") or []
                )
            }
            failed_action_lookup = {
                action_id.lower()
                for action_id in _dedupe_string_sequence(
                    custom_workflow_execution.get("failed_action_ids") or []
                )
            }
            raw_observations = custom_workflow_execution.get("action_observations")
            if isinstance(raw_observations, list):
                for raw_observation in raw_observations:
                    if not isinstance(raw_observation, Mapping):
                        continue
                    action_id = _safe_str(raw_observation.get("action_id"))
                    if not action_id:
                        continue
                    raw_status = (
                        _safe_str(raw_observation.get("status"))
                        or _safe_str(raw_observation.get("outcome"))
                    ).lower()
                    successful = (
                        raw_status == "success"
                        or action_id.lower() in successful_action_lookup
                    )
                    failed = raw_status in {"failed", "failure", "error"} or (
                        action_id.lower() in failed_action_lookup
                    )
                    if not successful and not failed:
                        continue
                    if successful:
                        _append_tool_name_once(augmented_successful_tools, action_id)
                    elif failed:
                        _append_tool_name_once(augmented_failed_tools, action_id)
                    _append_tool_name_once(observed_equivalent_tools, action_id)
                    _append_execution_surface_observation_once(
                        observed_execution_surfaces,
                        tool_name=action_id,
                        status="success" if successful else "failed",
                        source="workflow_action_execution",
                        workflow_id=(
                            _safe_str(raw_observation.get("workflow_id")) or workflow_id
                        ),
                        state_id=_safe_str(raw_observation.get("state_id")),
                        action_id=action_id,
                        details=raw_observation,
                    )

    return (
        augmented_successful_tools,
        augmented_failed_tools,
        observed_equivalent_tools,
        observed_execution_surfaces,
    )


def _observed_invocation_tool_names(
    serialised_invocations: Sequence[Mapping[str, Any]],
) -> list[str]:
    observed: list[str] = []
    seen: set[str] = set()
    for invocation in serialised_invocations:
        if not isinstance(invocation, Mapping):
            continue
        tool_name = _safe_str(invocation.get("tool")) or _safe_str(
            invocation.get("method")
        )
        if not tool_name:
            continue
        canonical_key = _tool_requirement_key(tool_name)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        observed.append(tool_name)
    return observed


def _required_effect_tool_obligation_summary(
    *,
    required_effects: Sequence[Mapping[str, Any]],
    serialised_invocations: Sequence[Mapping[str, Any]],
    observed_equivalent_tools: Sequence[str] | None = None,
) -> dict[str, Any]:
    observed_tool_names = _dedupe_string_sequence(
        [
            *_observed_invocation_tool_names(serialised_invocations),
            *(observed_equivalent_tools or ()),
        ]
    )
    observed_lookup = {_tool_requirement_key(tool) for tool in observed_tool_names}

    required_tools: list[str] = []
    missing_tools: list[str] = []
    unresolved_effect_ids: list[str] = []
    unresolved_effect_types: list[str] = []
    required_tool_seen: set[str] = set()
    missing_tool_seen: set[str] = set()
    unresolved_effect_seen: set[str] = set()
    unresolved_type_seen: set[str] = set()

    for effect in required_effects:
        if not isinstance(effect, Mapping):
            continue
        effect_required_tools = _dedupe_string_sequence(
            effect.get("required_tools") or []
        )
        for tool_name in effect_required_tools:
            lowered_tool = _tool_requirement_key(tool_name)
            if lowered_tool not in required_tool_seen:
                required_tool_seen.add(lowered_tool)
                required_tools.append(tool_name)

        effect_status = (_safe_str(effect.get("status")) or "").lower()
        if effect_status not in {"not_executed", "not_satisfied"}:
            continue

        effect_id = _safe_str(effect.get("effect_id"))
        if effect_id and effect_id.lower() not in unresolved_effect_seen:
            unresolved_effect_seen.add(effect_id.lower())
            unresolved_effect_ids.append(effect_id)
        effect_type = _safe_str(effect.get("effect_type"))
        if effect_type and effect_type.lower() not in unresolved_type_seen:
            unresolved_type_seen.add(effect_type.lower())
            unresolved_effect_types.append(effect_type)

        for tool_name in effect_required_tools:
            lowered_tool = _tool_requirement_key(tool_name)
            if lowered_tool in observed_lookup or lowered_tool in missing_tool_seen:
                continue
            missing_tool_seen.add(lowered_tool)
            missing_tools.append(tool_name)

    return {
        "required_effects_observed_tool_names": observed_tool_names,
        "required_effects_required_tools": required_tools,
        "required_effects_required_tool_count": len(required_tools),
        "required_effects_missing_required_tools": missing_tools,
        "required_effects_missing_required_tool_count": len(missing_tools),
        "required_effects_unresolved_effect_ids": unresolved_effect_ids,
        "required_effects_unresolved_effect_types": unresolved_effect_types,
    }


def _apply_required_effect_tool_obligations_to_execution_summary(
    *,
    execution_summary: Mapping[str, Any],
    required_effects: Sequence[Mapping[str, Any]],
    serialised_invocations: Sequence[Mapping[str, Any]],
    observed_equivalent_tools: Sequence[str] | None = None,
) -> dict[str, Any]:
    summary = dict(execution_summary)
    obligation_summary = _required_effect_tool_obligation_summary(
        required_effects=required_effects,
        serialised_invocations=serialised_invocations,
        observed_equivalent_tools=observed_equivalent_tools,
    )
    if obligation_summary["required_effects_required_tool_count"] <= 0:
        return summary

    summary.update(obligation_summary)
    tool_execution_raw = summary.get("tool_execution")
    tool_execution = (
        dict(tool_execution_raw) if isinstance(tool_execution_raw, Mapping) else {}
    )
    tool_execution.update(obligation_summary)
    summary["tool_execution"] = tool_execution

    missing_tool_count = int(
        obligation_summary["required_effects_missing_required_tool_count"]
    )
    zero_tools_executed = bool(summary.get("zero_tools_executed"))
    if missing_tool_count > 0 and zero_tools_executed:
        summary["zero_tool_execution_expected"] = False
        summary["zero_tool_reason_code"] = "required_effect_tools_missing"
        summary["zero_tool_reason"] = (
            "Required effect tools were declared but matching tool execution "
            "was not observed."
        )
        tool_execution["zero_tools_executed"] = True
        tool_execution["zero_tool_execution_expected"] = False
        tool_execution["zero_tool_reason_code"] = summary["zero_tool_reason_code"]
        tool_execution["zero_tool_reason"] = summary["zero_tool_reason"]

    return summary


def _normalise_prompt_provenance(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    render_variables_raw = value.get("render_variables")
    render_variables: dict[str, Any] = {}
    if isinstance(render_variables_raw, Mapping):
        for key, raw_value in render_variables_raw.items():
            if not isinstance(key, str):
                continue
            if isinstance(raw_value, str):
                render_variables[key] = _build_text_capture(raw_value)
            else:
                render_variables[key] = raw_value

    payload: dict[str, Any] = {
        "prompt_mode": _safe_str(value.get("prompt_mode")),
        "resolved_prompt_id": _safe_str(value.get("resolved_prompt_id")),
        "truncated": (
            bool(value.get("truncated"))
            if isinstance(value.get("truncated"), bool)
            else None
        ),
        "requested_prompt_ids": _extract_workflow_ids(
            value.get("requested_prompt_ids")
        ),
        "render_variables": render_variables or None,
    }
    return {key: item for key, item in payload.items() if item is not None}


def _normalise_llm_request(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    raw_prompt = value.get("prompt")
    context_messages: list[dict[str, Any]] = []
    raw_context_messages = value.get("context_messages")
    if isinstance(raw_context_messages, list):
        for raw_message in raw_context_messages:
            if not isinstance(raw_message, Mapping):
                continue
            entry: dict[str, Any] = {}
            for field_name in ("role", "name", "tool_call_id"):
                field_value = _safe_str(raw_message.get(field_name))
                if field_value:
                    entry[field_name] = field_value
            content_value = raw_message.get("content")
            if isinstance(content_value, Mapping):
                entry["content"] = dict(content_value)
            elif content_value is not None:
                entry["content"] = _build_text_capture(content_value)
            if entry:
                context_messages.append(entry)

    raw_context_summary = value.get("context_summary")
    context_summary = (
        {
            str(key): nested_value
            for key, nested_value in raw_context_summary.items()
            if isinstance(key, str)
        }
        if isinstance(raw_context_summary, Mapping)
        else None
    )
    raw_context_lineage = value.get("context_lineage")
    context_lineage = (
        {
            str(key): nested_value
            for key, nested_value in raw_context_lineage.items()
            if isinstance(key, str)
        }
        if isinstance(raw_context_lineage, Mapping)
        else None
    )

    payload: dict[str, Any] = {
        "prompt": (
            dict(raw_prompt)
            if isinstance(raw_prompt, Mapping)
            else _build_text_capture(raw_prompt)
        ),
        "context_messages": context_messages or None,
        "context_message_count": _safe_non_negative_int(
            value.get("context_message_count")
        ),
        "context_summary": context_summary,
        "context_lineage": context_lineage,
        "tool_names": _dedupe_string_sequence(value.get("tool_names") or []),
        "tool_count": _safe_non_negative_int(value.get("tool_count")),
        "workflow_action_id": _safe_str(value.get("workflow_action_id")),
        "required_prompt_tools": _dedupe_string_sequence(
            value.get("required_prompt_tools") or []
        ),
    }
    return {key: item for key, item in payload.items() if item is not None}


def _normalise_llm_call_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    exchange_blob_ref = value.get("exchange_blob_ref")
    candidate = value.get("candidate")
    payload: dict[str, Any] = {
        "call_type": _safe_str(value.get("type")) or _safe_str(value.get("call_type")),
        "stage": _safe_str(value.get("stage")),
        "workflow_stage_id": _safe_str(value.get("workflow_stage_id")),
        "model": _safe_str(value.get("model")) or _safe_str(value.get("model_name")),
        "provider": _safe_str(value.get("provider")),
        "duration_ms": _safe_non_negative_int(value.get("duration_ms")),
        "note": _safe_str(value.get("note")),
        "exchange_blob_ref": (
            {
                str(key): item
                for key, item in exchange_blob_ref.items()
                if isinstance(key, str)
            }
            if isinstance(exchange_blob_ref, Mapping)
            else None
        ),
        "candidate": (
            {str(key): item for key, item in candidate.items() if isinstance(key, str)}
            if isinstance(candidate, Mapping)
            else None
        ),
    }
    usage = value.get("usage")
    if isinstance(usage, Mapping):
        payload["usage"] = {
            str(key): item for key, item in usage.items() if isinstance(key, str)
        }
    return {key: item for key, item in payload.items() if item not in (None, [], {})}


def _normalise_projection_field_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    entries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        entry: dict[str, Any] = {
            "field_concept_id": _safe_str(item.get("field_concept_id")),
            "output_key": _safe_str(item.get("output_key")),
            "location": _safe_str(item.get("location")),
            "reason": _safe_str(item.get("reason")),
        }
        if "item_count" in item:
            entry["item_count"] = _safe_non_negative_int(item.get("item_count"))
        if "row_index" in item:
            raw_row_index = _safe_non_negative_int(item.get("row_index"))
            entry["row_index"] = min(raw_row_index, 1_000_000_000)
            if raw_row_index > 1_000_000_000:
                entry["row_index_clamped"] = True
        entry = {
            key: nested for key, nested in entry.items() if nested not in (None, [], {})
        }
        if entry:
            entries.append(entry)
    return entries


def _projection_field_ids(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    field_concept_ids: list[Any] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            field_concept_ids.append(entry.get("field_concept_id"))
    return _dedupe_string_sequence(field_concept_ids)


def _projection_payload_key_is_sensitive(key: str | None) -> bool:
    lowered = str(key or "").strip().lower()
    return bool(lowered) and any(
        marker in lowered for marker in _FINAL_ANSWER_PROJECTION_SECRET_KEY_PARTS
    )


def _compact_final_answer_projection_payload(
    value: Any,
    *,
    max_depth: int = _FINAL_ANSWER_PROJECTION_PAYLOAD_MAX_DEPTH,
    max_items: int = _FINAL_ANSWER_PROJECTION_PAYLOAD_MAX_ITEMS,
    max_string_chars: int = _FINAL_ANSWER_PROJECTION_PAYLOAD_MAX_STRING_CHARS,
    _depth: int = 0,
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        return text[:max_string_chars] + "..." if len(text) > max_string_chars else text
    if _depth >= max_depth:
        if isinstance(value, Mapping):
            compact: dict[str, Any] = {}
            for key, item in list(value.items())[:max_items]:
                if not isinstance(key, str):
                    continue
                if _projection_payload_key_is_sensitive(key):
                    compact[key] = "[redacted]"
                    continue
                if isinstance(item, (str, bool, int, float)) or item is None:
                    compact[key] = _compact_final_answer_projection_payload(
                        item,
                        max_depth=max_depth,
                        max_items=max_items,
                        max_string_chars=max_string_chars,
                        _depth=_depth + 1,
                    )
            omitted = max(0, len(value) - len(compact))
            if omitted:
                compact["_omitted_items"] = omitted
            return compact or {"_truncated": "mapping"}
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return ["_truncated"]
        return repr(value)[:max_string_chars]
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        visible_items = [
            (key, item)
            for key, item in value.items()
            if isinstance(key, str) and key != "_tool_evidence_projection"
        ]
        for key, item in visible_items[:max_items]:
            if not isinstance(key, str):
                continue
            if _projection_payload_key_is_sensitive(key):
                compact[key] = "[redacted]"
                continue
            compact_value = _compact_final_answer_projection_payload(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_string_chars=max_string_chars,
                _depth=_depth + 1,
            )
            if compact_value is not None:
                compact[key] = compact_value
        omitted = max(0, len(visible_items) - len(compact))
        if omitted:
            compact["_omitted_items"] = omitted
        return compact
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        compact_items = [
            _compact_final_answer_projection_payload(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_string_chars=max_string_chars,
                _depth=_depth + 1,
            )
            for item in list(value)[:max_items]
        ]
        omitted = max(0, len(value) - len(compact_items))
        if omitted:
            compact_items.append({"_omitted_items": omitted})
        return compact_items
    return repr(value)[:max_string_chars]


def _extract_projection_from_context_text(
    text: str,
    *,
    context_message_index: int,
) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    if not isinstance(parsed, Mapping):
        return None
    payload = parsed.get("payload")
    if not isinstance(payload, Mapping):
        return None
    projection = payload.get("_tool_evidence_projection")
    if not isinstance(projection, Mapping):
        return None

    preserved_fields = _normalise_projection_field_entries(
        projection.get("preserved_fields")
    )
    missing_required_fields = _normalise_projection_field_entries(
        projection.get("missing_required_fields")
    )
    omitted_fields = _normalise_projection_field_entries(
        projection.get("omitted_fields")
    )
    redacted_fields = _normalise_projection_field_entries(
        projection.get("redacted_fields")
    )
    entry: dict[str, Any] = {
        "context_message_index": context_message_index,
        "tool": _safe_str(parsed.get("tool")),
        "source_tool_invocation_id": _safe_str(parsed.get("call_id"))
        or _safe_str(parsed.get("tool_call_id"))
        or _safe_str(payload.get("call_id")),
        "tool_concept_id": _safe_str(projection.get("tool_concept_id")),
        "evidence_view_concept_ids": _dedupe_string_sequence(
            projection.get("evidence_view_concept_ids") or []
        ),
        "preserved_fields": preserved_fields,
        "missing_required_fields": missing_required_fields,
        "omitted_fields": omitted_fields,
        "redacted_fields": redacted_fields,
    }
    projected_payload = _compact_final_answer_projection_payload(payload)
    if isinstance(projected_payload, Mapping) and projected_payload:
        entry["projected_payload"] = dict(projected_payload)
    return {key: item for key, item in entry.items() if item not in (None, [], {})}


def _extract_tool_evidence_projection_reachability(
    request: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(request, Mapping):
        return None
    raw_messages = request.get("context_messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        return None

    entries: list[dict[str, Any]] = []
    for index, message in enumerate(raw_messages):
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        text = None
        if isinstance(content, Mapping):
            text = _safe_str(content.get("text"))
        elif isinstance(content, str):
            text = content
        if not text:
            continue
        projection_entry = _extract_projection_from_context_text(
            text,
            context_message_index=index,
        )
        if projection_entry is not None:
            source_tool_call_id = _safe_str(message.get("tool_call_id"))
            if source_tool_call_id and not projection_entry.get(
                "source_tool_invocation_id"
            ):
                projection_entry["source_tool_invocation_id"] = source_tool_call_id
            entries.append(projection_entry)

    if not entries:
        return None

    preserved_field_ids: list[str] = []
    missing_required_field_ids: list[str] = []
    omitted_field_ids: list[str] = []
    redacted_field_ids: list[str] = []
    for entry in entries:
        preserved_field_ids.extend(
            _projection_field_ids(entry.get("preserved_fields") or [])
        )
        missing_required_field_ids.extend(
            _projection_field_ids(entry.get("missing_required_fields") or [])
        )
        omitted_field_ids.extend(
            _projection_field_ids(entry.get("omitted_fields") or [])
        )
        redacted_field_ids.extend(
            _projection_field_ids(entry.get("redacted_fields") or [])
        )

    tool_names: list[Any] = []
    tool_concept_ids: list[Any] = []
    source_tool_invocation_ids: list[Any] = []
    for entry in entries:
        tool_names.append(entry.get("tool"))
        tool_concept_ids.append(entry.get("tool_concept_id"))
        source_tool_invocation_ids.append(entry.get("source_tool_invocation_id"))

    return {
        "schema_version": TOOL_EVIDENCE_PROJECTION_REACHABILITY_SCHEMA_VERSION,
        "projection_count": len(entries),
        "tools": _dedupe_string_sequence(tool_names),
        "tool_concept_ids": _dedupe_string_sequence(tool_concept_ids),
        "source_tool_invocation_ids": _dedupe_string_sequence(
            source_tool_invocation_ids
        ),
        "preserved_field_concept_ids": _dedupe_string_sequence(preserved_field_ids),
        "missing_required_field_concept_ids": _dedupe_string_sequence(
            missing_required_field_ids
        ),
        "omitted_field_concept_ids": _dedupe_string_sequence(omitted_field_ids),
        "redacted_field_concept_ids": _dedupe_string_sequence(redacted_field_ids),
        "entries": entries,
    }


def _bounded_projection_identifier_sequence(
    value: Any,
    *,
    max_items: int = _FINAL_ANSWER_PUBLIC_PROJECTION_MAX_IDENTIFIERS,
) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return []
    identifiers: list[str] = []
    seen: set[str] = set()
    for raw in value:
        text = _safe_str(raw)
        if not text or text in seen:
            continue
        seen.add(text)
        identifiers.append(
            str(
                _compact_final_answer_projection_payload(
                    text,
                    max_string_chars=_FINAL_ANSWER_PROJECTION_PAYLOAD_MAX_STRING_CHARS,
                )
            )
        )
        if len(identifiers) >= max_items:
            break
    return identifiers


def _bounded_public_projection_field_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return []
    bounded: list[dict[str, Any]] = []
    for raw_entry in _normalise_projection_field_entries(
        value[:_FINAL_ANSWER_PUBLIC_PROJECTION_MAX_FIELD_ENTRIES]
    ):
        compact_entry = _compact_final_answer_projection_payload(
            raw_entry,
            max_depth=2,
            max_items=8,
        )
        if isinstance(compact_entry, Mapping) and compact_entry:
            bounded.append(dict(compact_entry))
    return bounded


def project_final_answer_tool_evidence(
    turn_record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return bounded semantic tool evidence safe for external evaluation.

    The complete final-answer synthesis record contains prompts, context, model
    exchanges, and other private diagnostic content.  External evaluators need
    only the represented tool-evidence view that the final-answer model saw.
    This projection therefore fail-closes on an unknown schema, whitelists the
    reachability fields, caps all collections and strings, and reapplies the
    secret-key redaction used when the turn record is built.
    """

    if not isinstance(turn_record, Mapping):
        return None
    synthesis = turn_record.get("final_answer_synthesis")
    if not isinstance(synthesis, Mapping):
        return None
    raw_projection = synthesis.get("tool_evidence_projection")
    if (
        not isinstance(raw_projection, Mapping)
        or raw_projection.get("schema_version")
        != TOOL_EVIDENCE_PROJECTION_REACHABILITY_SCHEMA_VERSION
    ):
        return None
    raw_entries = raw_projection.get("entries")
    if not isinstance(raw_entries, Sequence) or isinstance(
        raw_entries,
        (str, bytes, bytearray),
    ):
        return None

    entries: list[dict[str, Any]] = []
    for raw_entry in raw_entries[:_FINAL_ANSWER_PUBLIC_PROJECTION_MAX_ENTRIES]:
        if not isinstance(raw_entry, Mapping):
            continue
        entry: dict[str, Any] = {}
        if "context_message_index" in raw_entry:
            entry["context_message_index"] = min(
                _safe_non_negative_int(raw_entry.get("context_message_index")),
                1_000_000_000,
            )
        for key in ("tool", "source_tool_invocation_id", "tool_concept_id"):
            text = _safe_str(raw_entry.get(key))
            if text:
                entry[key] = _compact_final_answer_projection_payload(text)
        evidence_view_concept_ids = _bounded_projection_identifier_sequence(
            raw_entry.get("evidence_view_concept_ids")
        )
        if evidence_view_concept_ids:
            entry["evidence_view_concept_ids"] = evidence_view_concept_ids
        for key in (
            "preserved_fields",
            "missing_required_fields",
            "omitted_fields",
            "redacted_fields",
        ):
            fields = _bounded_public_projection_field_entries(raw_entry.get(key))
            if fields:
                entry[key] = fields
        if "projected_payload" in raw_entry:
            projected_payload = _compact_final_answer_projection_payload(
                raw_entry.get("projected_payload")
            )
            if projected_payload not in (None, [], {}):
                entry["projected_payload"] = projected_payload
        if entry:
            entries.append(entry)

    if not entries:
        return None

    source_projection_count = min(
        max(
            _safe_non_negative_int(raw_projection.get("projection_count")),
            len(raw_entries),
        ),
        1_000_000_000,
    )
    included_projection_count = len(entries)
    projection: dict[str, Any] = {
        "schema_version": TOOL_EVIDENCE_PROJECTION_REACHABILITY_SCHEMA_VERSION,
        "projection_count": source_projection_count,
        "included_projection_count": included_projection_count,
        "omitted_projection_count": max(
            0,
            source_projection_count - included_projection_count,
        ),
        "entries": entries,
    }
    for key in (
        "tools",
        "tool_concept_ids",
        "source_tool_invocation_ids",
        "preserved_field_concept_ids",
        "missing_required_field_concept_ids",
        "omitted_field_concept_ids",
        "redacted_field_concept_ids",
    ):
        identifiers = _bounded_projection_identifier_sequence(raw_projection.get(key))
        if identifiers:
            projection[key] = identifiers
    return projection


def _field_lineage_entries_from_projection_entry(
    entry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    lineage_entries: list[dict[str, Any]] = []
    field_sets = (
        ("preserved_fields", "satisfied"),
        ("missing_required_fields", "unresolved"),
        ("omitted_fields", "omitted"),
        ("redacted_fields", "redacted"),
    )
    for field_key, status in field_sets:
        raw_fields = entry.get(field_key)
        if not isinstance(raw_fields, Sequence) or isinstance(
            raw_fields, (str, bytes, bytearray)
        ):
            continue
        for field in raw_fields:
            if not isinstance(field, Mapping):
                continue
            field_concept_id = _safe_str(field.get("field_concept_id"))
            output_key = _safe_str(field.get("output_key"))
            if not field_concept_id and not output_key:
                continue
            field_entry: dict[str, Any] = {
                "field_concept_id": field_concept_id,
                "output_key": output_key,
                "status": status,
                "source": "final_answer_tool_evidence_projection",
                "tool": _safe_str(entry.get("tool")),
                "tool_concept_id": _safe_str(entry.get("tool_concept_id")),
                "source_tool_invocation_id": _safe_str(
                    entry.get("source_tool_invocation_id")
                ),
                "evidence_view_concept_ids": _dedupe_string_sequence(
                    entry.get("evidence_view_concept_ids") or []
                ),
                "reason": _safe_str(field.get("reason")),
            }
            lineage_entries.append(
                {
                    key: value
                    for key, value in field_entry.items()
                    if value not in (None, [], {})
                }
            )
    return lineage_entries


def _requested_field_status_counts(
    fields: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts = {
        "satisfied": 0,
        "unresolved": 0,
        "omitted": 0,
        "redacted": 0,
    }
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        status = _safe_str(field.get("status"))
        if status in counts:
            counts[status] += 1
    return counts


def _final_response_lineage_payload(response_text: Any) -> dict[str, Any]:
    text = response_text if isinstance(response_text, str) else str(response_text or "")
    return {
        "source": "final_visible_response",
        "text_checked_sha256": _hash_text(text),
        "text_checked_char_count": len(text),
        "text_checked_preview": text[:1000],
    }


def _effect_resolver_chain_payload(
    effect: Mapping[str, Any],
    *,
    contract_id: str | None,
    contract_source: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "effect_id": _safe_str(effect.get("effect_id")),
        "effect_type": _safe_str(effect.get("effect_type")),
        "status": _safe_str(effect.get("status")),
        "status_reason": _safe_str(effect.get("status_reason")),
        "required_tools": _dedupe_string_sequence(effect.get("required_tools") or []),
        "required_tools_match": _safe_str(effect.get("required_tools_match")),
        "targets": _dedupe_string_sequence(effect.get("targets") or []),
        "failure_codes": _normalise_failure_codes(effect.get("failure_codes")),
        "failure_code": _safe_str(effect.get("failure_code")),
        "source": _safe_str(effect.get("source")) or contract_source,
        "intent_origin": _safe_str(effect.get("intent_origin")),
        "represented_contract_id": contract_id,
        "represented_contract_source": contract_source,
    }
    return {key: item for key, item in payload.items() if item not in (None, [], {})}


def _build_requested_evidence_lineage(
    *,
    response_text: Any,
    final_answer_synthesis: Mapping[str, Any] | None,
    required_effects: Sequence[Mapping[str, Any]],
    turn_expected_outcome_contract: Mapping[str, Any] | None,
    prompt_required_evidence_contract: Mapping[str, Any] | None,
    workflow_required_effects_contract: Mapping[str, Any] | None,
    workflow_required_effects_contract_source: str | None,
    completion_gate: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    tool_projection = (
        final_answer_synthesis.get("tool_evidence_projection")
        if isinstance(final_answer_synthesis, Mapping)
        else None
    )
    projection_entries = (
        tool_projection.get("entries") if isinstance(tool_projection, Mapping) else None
    )
    requested_fields: list[dict[str, Any]] = []
    if isinstance(projection_entries, Sequence) and not isinstance(
        projection_entries, (str, bytes, bytearray)
    ):
        for projection_entry in projection_entries:
            if isinstance(projection_entry, Mapping):
                requested_fields.extend(
                    _field_lineage_entries_from_projection_entry(projection_entry)
                )

    expected_required_tools = _dedupe_string_sequence(
        (
            turn_expected_outcome_contract.get("required_tools")
            if isinstance(turn_expected_outcome_contract, Mapping)
            else []
        )
        or []
    )
    expected_target_concept_ids = _dedupe_string_sequence(
        (
            turn_expected_outcome_contract.get("target_concept_ids")
            if isinstance(turn_expected_outcome_contract, Mapping)
            else []
        )
        or []
    )

    prompt_contract_id = (
        _safe_str(prompt_required_evidence_contract.get("contract_id"))
        if isinstance(prompt_required_evidence_contract, Mapping)
        else None
    )
    workflow_contract_id = (
        _safe_str(workflow_required_effects_contract.get("contract_id"))
        if isinstance(workflow_required_effects_contract, Mapping)
        else None
    )
    evidence_view_concept_ids = _dedupe_string_sequence(
        [
            evidence_view_id
            for field in requested_fields
            if isinstance(field, Mapping)
            for evidence_view_id in (field.get("evidence_view_concept_ids") or [])
        ]
    )
    represented_contract_ids = _dedupe_string_sequence(
        [
            prompt_contract_id,
            workflow_contract_id,
            *evidence_view_concept_ids,
        ]
    )

    resolver_chains: list[dict[str, Any]] = []
    for effect in required_effects:
        if not isinstance(effect, Mapping):
            continue
        effect_type = _safe_str(effect.get("effect_type"))
        if not (
            _is_evidence_effect_type(effect_type)
            or effect_type in {"tool_execution", "workflow_execution"}
        ):
            continue
        contract_id = (
            workflow_contract_id
            if _safe_str(effect.get("source")) == "workflow_required_effects_contract"
            else prompt_contract_id
        )
        contract_source = (
            workflow_required_effects_contract_source
            if contract_id == workflow_contract_id
            else _safe_str(effect.get("source"))
        )
        resolver_chains.append(
            _effect_resolver_chain_payload(
                effect,
                contract_id=contract_id,
                contract_source=contract_source,
            )
        )

    field_counts = _requested_field_status_counts(requested_fields)
    unresolved_fields = [
        dict(field)
        for field in requested_fields
        if isinstance(field, Mapping) and field.get("status") == "unresolved"
    ]
    unresolved_resolvers = [
        dict(chain)
        for chain in resolver_chains
        if isinstance(chain, Mapping)
        and _safe_str(chain.get("status")) in {"not_satisfied", "not_executed"}
    ]
    gate_payload = dict(completion_gate) if isinstance(completion_gate, Mapping) else {}
    gate_evidence = (
        gate_payload.get("evidence_payload")
        if isinstance(gate_payload.get("evidence_payload"), Mapping)
        else {}
    )
    answer_consistency_blocker = (
        gate_evidence.get("required_evidence_answer_consistency_blocker")
        if isinstance(gate_evidence, Mapping)
        else None
    )

    if not any(
        [
            requested_fields,
            resolver_chains,
            expected_required_tools,
            expected_target_concept_ids,
            represented_contract_ids,
            isinstance(tool_projection, Mapping),
        ]
    ):
        return None

    lineage: dict[str, Any] = {
        "schema_version": REQUESTED_EVIDENCE_LINEAGE_SCHEMA_VERSION,
        "final_response": _final_response_lineage_payload(response_text),
        "turn_expected_required_tools": expected_required_tools,
        "turn_expected_target_concept_ids": expected_target_concept_ids,
        "represented_contract_ids": represented_contract_ids,
        "requested_fields": requested_fields,
        "requested_field_status_counts": field_counts,
        "unresolved_requested_fields": unresolved_fields,
        "resolver_chains": resolver_chains,
        "unresolved_resolver_chains": unresolved_resolvers,
        "final_answer_projection": (
            {
                "projection_count": _safe_non_negative_int(
                    tool_projection.get("projection_count")
                ),
                "tools": _dedupe_string_sequence(tool_projection.get("tools") or []),
                "tool_concept_ids": _dedupe_string_sequence(
                    tool_projection.get("tool_concept_ids") or []
                ),
                "source_tool_invocation_ids": _dedupe_string_sequence(
                    tool_projection.get("source_tool_invocation_ids") or []
                ),
                "preserved_field_concept_ids": _dedupe_string_sequence(
                    tool_projection.get("preserved_field_concept_ids") or []
                ),
                "missing_required_field_concept_ids": _dedupe_string_sequence(
                    tool_projection.get("missing_required_field_concept_ids") or []
                ),
                "omitted_field_concept_ids": _dedupe_string_sequence(
                    tool_projection.get("omitted_field_concept_ids") or []
                ),
                "redacted_field_concept_ids": _dedupe_string_sequence(
                    tool_projection.get("redacted_field_concept_ids") or []
                ),
            }
            if isinstance(tool_projection, Mapping)
            else None
        ),
        "answer_consistency_blocker": (
            dict(answer_consistency_blocker)
            if isinstance(answer_consistency_blocker, Mapping)
            else None
        ),
        "completion_gate_decision": _safe_str(gate_payload.get("decision")),
        "completion_gate_safe_to_claim_completion": (
            gate_payload.get("safe_to_claim_completion")
            if isinstance(gate_payload.get("safe_to_claim_completion"), bool)
            else None
        ),
    }
    return {key: item for key, item in lineage.items() if item not in (None, [], {})}


def _select_latest_summariser_request_entry(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    entries = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_model_policy_stage",
    )
    final_answer_fallback: dict[str, Any] | None = None
    for entry in reversed(entries):
        stage = (_safe_str(entry.get("stage")) or "").lower()
        if stage == "summariser":
            return entry
        if final_answer_fallback is None and _entry_targets_final_answer_synthesis(
            entry
        ):
            final_answer_fallback = entry
    return final_answer_fallback


def _select_latest_summariser_llm_call(
    llm_calls: Sequence[Mapping[str, Any]] | None,
    *,
    stage: str | None,
    workflow_stage_id: str | None,
) -> dict[str, Any] | None:
    fallback: Mapping[str, Any] | None = None
    preferred_stages = {"summariser"}
    stage_lower = (stage or "").strip().lower()
    if stage_lower:
        preferred_stages.add(stage_lower)
    for entry in reversed(llm_calls or ()):
        if not isinstance(entry, Mapping):
            continue
        stage = (_safe_str(entry.get("stage")) or "").lower()
        call_type = (
            _safe_str(entry.get("type")) or _safe_str(entry.get("call_type")) or ""
        )
        if stage not in preferred_stages or not call_type.startswith("llm.generate"):
            continue
        entry_workflow_stage_id = _safe_str(entry.get("workflow_stage_id"))
        if workflow_stage_id and entry_workflow_stage_id == workflow_stage_id:
            return _normalise_llm_call_entry(entry)
        if fallback is None:
            fallback = entry
    return _normalise_llm_call_entry(fallback) if fallback is not None else None


def _request_prompt_text(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    prompt = value.get("prompt")
    if isinstance(prompt, Mapping):
        return _safe_str(prompt.get("text")) or _safe_str(prompt.get("preview"))
    return _safe_str(prompt)


def _request_prompt_concept_ids(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    prompt_ids: list[Any] = []
    for key in ("requested_prompt_ids", "requested_prompt_concept_ids"):
        raw_ids = value.get(key)
        if isinstance(raw_ids, Sequence) and not isinstance(
            raw_ids, (str, bytes, bytearray)
        ):
            prompt_ids.extend(raw_ids)
    for key in ("prompt_id", "resolved_prompt_id", "resolved_prompt_concept_id"):
        prompt_ids.append(value.get(key))

    prompt = value.get("prompt")
    if isinstance(prompt, Mapping):
        for key in ("prompt_id", "resolved_prompt_id", "resolved_prompt_concept_id"):
            prompt_ids.append(prompt.get(key))
    return _dedupe_string_sequence(prompt_ids)


def _entry_targets_final_answer_synthesis(entry: Mapping[str, Any]) -> bool:
    request = entry.get("request")
    request_prompt_ids = set(_request_prompt_concept_ids(request))
    entry_prompt_ids = set(_request_prompt_concept_ids(entry))
    if (
        request_prompt_ids | entry_prompt_ids
    ) & _FINAL_ANSWER_SYNTHESIS_PROMPT_CONCEPT_IDS:
        return True

    prompt_text = _request_prompt_text(request)
    if not prompt_text:
        return False
    prompt_text_lower = prompt_text.lower()
    return any(
        marker.lower() in prompt_text_lower
        for marker in _FINAL_ANSWER_SYNTHESIS_PROMPT_MARKERS
    )


def _build_final_answer_synthesis_telemetry(
    *,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    llm_calls: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    request_entry = _select_latest_summariser_request_entry(aux_llm_calls)
    request = (
        _normalise_llm_request(request_entry.get("request"))
        if isinstance(request_entry, Mapping)
        else None
    )
    stage = (
        _safe_str(request_entry.get("stage"))
        if isinstance(request_entry, Mapping)
        else None
    ) or "summariser"
    workflow_stage_id = (
        _safe_str(request_entry.get("workflow_stage_id"))
        if isinstance(request_entry, Mapping)
        else None
    )
    llm_call = _select_latest_summariser_llm_call(
        llm_calls,
        stage=stage,
        workflow_stage_id=workflow_stage_id,
    )
    if request_entry is None and llm_call is None:
        return None

    tool_projection_reachability = _extract_tool_evidence_projection_reachability(
        request
    )
    selected = (
        request_entry.get("selected") if isinstance(request_entry, Mapping) else None
    )
    exchange_blob_ref = (
        llm_call.get("exchange_blob_ref") if isinstance(llm_call, Mapping) else None
    )
    context_lineage = (
        request.get("context_lineage") if isinstance(request, Mapping) else None
    )
    exchange_blob_ref_payload = (
        {
            str(key): item
            for key, item in exchange_blob_ref.items()
            if isinstance(key, str)
        }
        if isinstance(exchange_blob_ref, Mapping)
        else None
    )
    context_lineage_payload = (
        {
            str(key): item
            for key, item in context_lineage.items()
            if isinstance(key, str)
        }
        if isinstance(context_lineage, Mapping)
        else None
    )
    payload: dict[str, Any] = {
        "schema_version": FINAL_ANSWER_SYNTHESIS_TELEMETRY_SCHEMA_VERSION,
        "stage": stage,
        "workflow_stage_id": workflow_stage_id,
        "model": (
            _safe_str(llm_call.get("model")) if isinstance(llm_call, Mapping) else None
        )
        or (
            _safe_str(selected.get("model_resolved"))
            if isinstance(selected, Mapping)
            else None
        ),
        "provider": (
            _safe_str(llm_call.get("provider"))
            if isinstance(llm_call, Mapping)
            else None
        )
        or (
            _safe_str(selected.get("provider"))
            if isinstance(selected, Mapping)
            else None
        ),
        "llm_call": llm_call,
        "exchange_blob_ref": exchange_blob_ref_payload,
        "request": request,
        "context_lineage": context_lineage_payload,
        "tool_evidence_projection": tool_projection_reachability,
        "fallback_used": (
            bool(request_entry.get("fallback_used"))
            if isinstance(request_entry, Mapping)
            and isinstance(request_entry.get("fallback_used"), bool)
            else None
        ),
        "fallback_attempt_count": (
            _safe_non_negative_int(request_entry.get("fallback_attempt_count"))
            if isinstance(request_entry, Mapping)
            else None
        ),
        "failure_count": (
            _safe_non_negative_int(request_entry.get("failure_count"))
            if isinstance(request_entry, Mapping)
            else None
        ),
    }
    return {key: item for key, item in payload.items() if item not in (None, [], {})}


def _normalise_model_policy_attempts(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    attempts: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        entry: dict[str, Any] = {
            "attempt_no": _safe_non_negative_int(raw.get("attempt_no")),
            "provider": _safe_str(raw.get("provider")),
            "model": _safe_str(raw.get("model")),
            "status": _safe_str(raw.get("status")),
            "error": _safe_str(raw.get("error")),
            "error_class": _safe_str(raw.get("error_class")),
            "failure_kind": _safe_str(raw.get("failure_kind")),
            "duration_ms": _safe_non_negative_int(raw.get("duration_ms")),
            "raw_response_present": (
                bool(raw.get("raw_response_present"))
                if isinstance(raw.get("raw_response_present"), bool)
                else None
            ),
        }
        candidate = raw.get("candidate")
        if isinstance(candidate, Mapping):
            entry["candidate"] = dict(candidate)
        probe = raw.get("probe")
        if isinstance(probe, Mapping):
            entry["probe"] = dict(probe)
        response_capture = raw.get("response")
        if isinstance(response_capture, Mapping):
            entry["response"] = dict(response_capture)
        elif response_capture is not None:
            entry["response"] = _build_text_capture(response_capture)
        attempts.append({key: item for key, item in entry.items() if item is not None})
    return attempts


def _normalise_model_policy_errors(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    errors: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        entry: dict[str, Any] = {
            "model_resolved": _safe_str(raw.get("model_resolved")),
            "error": _safe_str(raw.get("error")),
            "error_class": _safe_str(raw.get("error_class")),
            "failure_kind": _safe_str(raw.get("failure_kind")),
        }
        candidate = raw.get("candidate")
        if isinstance(candidate, Mapping):
            entry["candidate"] = dict(candidate)
        errors.append({key: item for key, item in entry.items() if item is not None})
    return errors


def _derive_discovery_match_absence_reason(
    *,
    discovery_candidates: Sequence[Mapping[str, Any]],
    routing_matches: Sequence[Mapping[str, Any]],
    excluded_candidates: Sequence[Mapping[str, Any]],
    discovery_errors: Sequence[str] = (),
    explicit_match_absence_reason: str | None = None,
) -> str | None:
    if explicit_match_absence_reason:
        return explicit_match_absence_reason
    if routing_matches:
        return None
    if not discovery_candidates:
        error_set = {
            str(item).strip() for item in discovery_errors if str(item).strip()
        }
        if (
            "capability_index_wait_timed_out" in error_set
            and "capability_index_build_in_progress" in error_set
        ):
            return "capability_index_wait_timed_out_build_in_progress"
        if "capability_index_build_in_progress" in error_set:
            return "capability_index_build_in_progress"
        if "capability_index_not_ready" in error_set:
            return "capability_index_not_ready"
        if any(
            error.startswith("capability_index_build_error:") for error in error_set
        ):
            return "capability_index_build_error"
        return "no_discovery_candidates"
    if excluded_candidates and len(excluded_candidates) >= len(discovery_candidates):
        exclusion_counts = _count_named_values(
            excluded_candidates,
            field_name="routing_exclusion_reason",
        )
        if len(exclusion_counts) == 1:
            return next(iter(exclusion_counts.keys()))
        return "all_discovery_candidates_excluded"
    if excluded_candidates:
        return "no_routing_match_after_exclusions"
    return "no_routing_match_above_threshold"


def _normalise_workflow_candidate_details(values: Any) -> list[dict[str, Any]]:
    if values is None:
        return []

    if isinstance(values, Mapping):
        iterable: Sequence[Any] = (values,)
    elif isinstance(values, Sequence) and not isinstance(
        values, (str, bytes, bytearray)
    ):
        iterable = values
    else:
        iterable = (values,)

    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in iterable:
        if isinstance(raw, str):
            concept_id = _safe_str(raw)
            if not concept_id:
                continue
            lowered = concept_id.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalised.append({"concept_id": concept_id})
            continue

        if not isinstance(raw, Mapping):
            continue

        concept_id = (
            _safe_str(raw.get("concept_id"))
            or _safe_str(raw.get("workflow_id"))
            or _safe_str(raw.get("id"))
        )
        if not concept_id:
            continue
        lowered = concept_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)

        entry: dict[str, Any] = {"concept_id": concept_id}
        for field_name in (
            "name",
            "description",
            "match_source",
            "executability_reason",
            "executability_detail",
            "routing_exclusion_reason",
            "candidate_source",
            "candidate_reason",
        ):
            field_value = _safe_str(raw.get(field_name))
            if field_value:
                entry[field_name] = field_value
        for field_name in ("relevance_score", "confidence_score"):
            raw_value = raw.get(field_name)
            try:
                if raw_value is not None:
                    entry[field_name] = float(raw_value)
            except Exception:
                continue
        for field_name in ("is_executable", "is_policy_safe", "routing_eligible"):
            raw_value = raw.get(field_name)
            if isinstance(raw_value, bool):
                entry[field_name] = raw_value
        normalised.append(entry)

    return normalised


def _resolve_turn_expected_outcome_contract_snapshot(
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


def _activated_turn_expected_conditional_required_tools(
    *,
    contract: TurnExpectedOutcomeContract,
    tool_invocations: Sequence[Mapping[str, Any]] | None,
    obligation_carry_forward_permitted: bool | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return activated conditional tools plus the carry-forward projection.

    Conditional required tools only activate once a symbolic target is resolved.
    A prior-turn referent carried into this turn must not, by itself, activate the
    prior turn's obligations: that is how a bare "do you have a concept for X"
    follow-up inherited a full ingestion/read-back obligation family. The
    represented context-adjudication decision owns the carry-forward policy; this
    support surface enforces it and records what was suppressed.
    """

    return adjudicate_conditional_required_tool_activation(
        contract=contract,
        tool_invocations=tool_invocations,
        obligation_carry_forward_permitted=obligation_carry_forward_permitted,
    )


def _normalise_discovery_stage_timings(
    raw_stage_timings: Any,
    *,
    max_entries: int = 64,
    slowest_limit: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (bounded stage_timings list, slowest_stages summary).

    Stage timings are emitted by ``_record_discovery_stage_timing`` in the
    workflow discovery service. Surfacing them in routing diagnostics turns
    every replay/turn record into a self-describing latency report so that
    discovery slowness can be diagnosed without trawling server logs.
    """

    if not isinstance(raw_stage_timings, Sequence) or isinstance(
        raw_stage_timings, (str, bytes)
    ):
        return [], []

    cleaned: list[dict[str, Any]] = []
    for entry in raw_stage_timings:
        if not isinstance(entry, Mapping):
            continue
        normalised: dict[str, Any] = {}
        for key, value in entry.items():
            key_text = str(key or "").strip()
            if not key_text:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                normalised[key_text] = value
            elif isinstance(value, Mapping):
                scalar_map: dict[str, Any] = {}
                for map_key, map_value in value.items():
                    if (
                        isinstance(map_value, (str, int, float, bool))
                        or map_value is None
                    ):
                        scalar_map[str(map_key)[:120]] = map_value
                if scalar_map:
                    normalised[key_text] = scalar_map
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                normalised[key_text] = [str(item)[:240] for item in list(value)[:20]]
        if normalised:
            cleaned.append(normalised)
        if len(cleaned) >= max_entries:
            break

    def _elapsed(entry: Mapping[str, Any]) -> float:
        value = entry.get("elapsed_ms")
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    slowest = sorted(cleaned, key=_elapsed, reverse=True)[:slowest_limit]
    slowest_summary = [
        {
            "stage": entry.get("stage"),
            "status": entry.get("status"),
            "elapsed_ms": _elapsed(entry),
        }
        for entry in slowest
        if _elapsed(entry) > 0.0
    ]
    return cleaned, slowest_summary


def build_workflow_routing_diagnostics(
    *,
    workflow_discovery: Mapping[str, Any] | None,
    workflow_routing: Mapping[str, Any] | None,
    turn_execution_diagnostics: Mapping[str, Any] | None,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    turn_expected_outcome_contract: Mapping[str, Any] | None = None,
    execution_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    workflow_discovery_payload = (
        workflow_discovery if isinstance(workflow_discovery, Mapping) else {}
    )
    workflow_routing_payload = (
        workflow_routing if isinstance(workflow_routing, Mapping) else {}
    )
    resolved_turn_expected_outcome_contract = (
        _resolve_turn_expected_outcome_contract_snapshot(
            turn_expected_outcome_contract,
            workflow_discovery_payload,
            workflow_routing_payload,
        )
    )
    turn_expected_outcome_contract_payload = (
        resolved_turn_expected_outcome_contract.to_dict()
    )
    selector_prompt_entries = _collect_aux_entries(
        aux_llm_calls, entry_type="workflow_selector_prompt"
    )
    selector_entries = _collect_aux_entries(
        aux_llm_calls, entry_type="workflow_selector"
    )
    selector_entry = selector_entries[-1] if selector_entries else {}
    selector_prompt_entry = (
        selector_prompt_entries[-1] if selector_prompt_entries else {}
    )
    selector_override_entries = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_selector_override",
    )
    model_policy_entries = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_model_policy_stage",
    )
    selector_model_policy_entries = [
        entry
        for entry in model_policy_entries
        if (_safe_str(entry.get("policy_stage")) or "").lower() == "classifier"
        or (_safe_str(entry.get("stage")) or "").lower() == "workflow_dispatch"
    ]
    selector_model_policy_entry = (
        selector_model_policy_entries[-1] if selector_model_policy_entries else {}
    )
    dispatch_events = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_dispatch_boundary",
    )
    turn_contract_check_entries = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_dispatch_turn_contract_check",
    )
    dispatch_prepare_events = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_dispatch_prepare_step",
    )

    if isinstance(execution_summary, Mapping):
        execution_summary_payload = dict(execution_summary)
    else:
        execution_summary_payload = _summarise_tool_execution_context(
            workflow_routing=workflow_routing_payload,
            turn_execution_diagnostics=turn_execution_diagnostics,
            aux_llm_calls=aux_llm_calls,
            serialised_invocations=[],
        )

    def _selector_context_value(field_name: str) -> Any:
        selector_value = selector_entry.get(field_name)
        if selector_value not in (None, "", [], {}):
            return selector_value
        prompt_value = selector_prompt_entry.get(field_name)
        if prompt_value not in (None, "", [], {}):
            return prompt_value
        return None

    discovery_candidates = _normalise_workflow_candidate_details(
        workflow_discovery_payload.get("candidates")
    )
    if not discovery_candidates:
        discovery_candidates = _normalise_workflow_candidate_details(
            _selector_context_value("discovery_candidates")
        )
    routing_matches = _normalise_workflow_candidate_details(
        workflow_discovery_payload.get("routing_matches")
        or workflow_discovery_payload.get("matches")
    )
    selector_candidates = _normalise_workflow_candidate_details(
        _selector_context_value("candidate_entries")
    )
    excluded_candidates = _normalise_workflow_candidate_details(
        _selector_context_value("discovery_excluded_candidates")
    )
    if not excluded_candidates:
        excluded_candidates = [
            entry
            for entry in discovery_candidates
            if entry.get("routing_eligible") is False
            or _safe_str(entry.get("routing_exclusion_reason"))
        ]

    selector_prompt_capture = _selector_context_value("prompt")
    if not isinstance(selector_prompt_capture, Mapping):
        selector_prompt_capture = _build_text_capture(
            _selector_context_value("prompt_text")
        )
    selector_candidate_list_capture = _selector_context_value("candidate_list")
    if not isinstance(selector_candidate_list_capture, Mapping):
        selector_candidate_list_capture = _build_text_capture(
            _selector_context_value("candidate_list_text")
        )
    selector_response_capture = selector_entry.get("response")
    if not isinstance(selector_response_capture, Mapping):
        selector_response_capture = _build_text_capture(
            selector_entry.get("raw_response")
        )
    selector_prompt_present = isinstance(selector_prompt_capture, Mapping) and bool(
        _safe_str(selector_prompt_capture.get("text"))
        or selector_prompt_capture.get("char_count")
    )
    selector_candidate_list_present = isinstance(
        selector_candidate_list_capture, Mapping
    ) and bool(
        _safe_str(selector_candidate_list_capture.get("text"))
        or selector_candidate_list_capture.get("char_count")
    )
    selector_response_present = isinstance(selector_response_capture, Mapping) and bool(
        _safe_str(selector_response_capture.get("text"))
        or selector_response_capture.get("char_count")
    )
    selector_prompt_failure_reason = _safe_str(
        _selector_context_value("prompt_failure_reason")
    )
    selector_prompt_failure_detail = _safe_str(
        _selector_context_value("prompt_failure_detail")
    )
    selector_outcome_workflow_id = _safe_str(
        workflow_routing_payload.get("workflow_id")
    ) or _safe_str(selector_entry.get("workflow_id"))
    selector_outcome_verdict = _safe_str(
        workflow_routing_payload.get("verdict")
    ) or _safe_str(selector_entry.get("verdict"))
    if selector_outcome_workflow_id and (
        (selector_outcome_verdict or "").strip().lower()
        not in _SELECTOR_PROMPT_FAILURE_VERDICTS
    ):
        selector_prompt_failure_reason = None
        selector_prompt_failure_detail = None
    raw_policy_candidate_scores = (
        selector_entry.get("policy_candidate_scores")
        if selector_entry.get("policy_candidate_scores") is not None
        else selector_prompt_entry.get("policy_candidate_scores")
    )

    selector_selected_candidate = selector_entry.get("candidate")
    selected_candidate_payload = (
        dict(selector_selected_candidate)
        if isinstance(selector_selected_candidate, Mapping)
        else None
    )
    selector_prompt_provenance = _normalise_prompt_provenance(
        _selector_context_value("prompt_provenance")
    )
    selector_requested_prompt_ids = _extract_workflow_ids(
        _selector_context_value("requested_prompt_ids")
    )
    selector_selection_metadata: dict[str, Any] = (
        {
            str(key): value
            for key, value in selector_entry.get("selection_metadata", {}).items()
            if isinstance(key, str)
        }
        if isinstance(selector_entry.get("selection_metadata"), Mapping)
        else {}
    )
    selector_model_attempts = _normalise_model_policy_attempts(
        selector_model_policy_entry.get("fallback_attempts")
    )
    selector_model_errors = _normalise_model_policy_errors(
        selector_model_policy_entry.get("errors")
    )
    selector_model_request = _normalise_llm_request(
        selector_model_policy_entry.get("request")
    )
    selector_context_summary = (
        {
            str(key): value
            for key, value in selector_model_request.get("context_summary", {}).items()
            if isinstance(key, str)
        }
        if isinstance(selector_model_request, Mapping)
        and isinstance(selector_model_request.get("context_summary"), Mapping)
        else None
    )
    selector_context_lineage = (
        {
            str(key): value
            for key, value in selector_model_request.get("context_lineage", {}).items()
            if isinstance(key, str)
        }
        if isinstance(selector_model_request, Mapping)
        and isinstance(selector_model_request.get("context_lineage"), Mapping)
        else (
            {
                str(key): value
                for key, value in selector_entry.get("context_lineage", {}).items()
                if isinstance(key, str)
            }
            if isinstance(selector_entry.get("context_lineage"), Mapping)
            else None
        )
    )
    selector_missing_telemetry_fields: list[str] = []
    if not selector_prompt_present:
        selector_missing_telemetry_fields.append("prompt")
    if not selector_candidate_list_present:
        selector_missing_telemetry_fields.append("candidate_list")
    if not selector_response_present:
        selector_missing_telemetry_fields.append("response")
    if not selector_candidates:
        selector_missing_telemetry_fields.append("candidate_entries")
    if not isinstance(selector_context_lineage, Mapping):
        selector_missing_telemetry_fields.append("context_lineage")
    selector_response_absence_reason = None
    if not selector_response_present:
        if selector_prompt_entries or selector_entries:
            selector_response_absence_reason = "selector_response_not_captured"
        else:
            selector_response_absence_reason = "selector_aux_telemetry_unavailable"
    selected_model_candidate = (
        {
            str(key): value
            for key, value in selector_model_policy_entry.get("selected", {}).items()
            if isinstance(key, str)
        }
        if isinstance(selector_model_policy_entry.get("selected"), Mapping)
        else None
    )

    discovery_candidate_count = _safe_non_negative_int(
        workflow_discovery_payload.get("candidate_count")
    )
    if discovery_candidate_count <= 0:
        discovery_candidate_count = len(discovery_candidates)
    discovery_match_count = _safe_non_negative_int(
        workflow_discovery_payload.get("match_count")
    )
    if discovery_match_count <= 0:
        discovery_match_count = len(routing_matches)
    discovery_exclusion_reason_counts = _sorted_count_entries(
        _count_named_values(excluded_candidates, field_name="routing_exclusion_reason")
    )
    discovery_candidate_source_counts = _sorted_count_entries(
        _count_named_values(discovery_candidates, field_name="candidate_source")
    )
    selector_candidate_source_counts = _sorted_count_entries(
        _count_named_values(selector_candidates, field_name="candidate_source")
    )
    selector_candidate_reason_counts = _sorted_count_entries(
        _count_named_values(selector_candidates, field_name="candidate_reason")
    )
    selector_fallback_failure_kind_counts = _sorted_count_entries(
        _count_named_values(selector_model_errors, field_name="failure_kind")
    )
    discovery_errors = _dedupe_string_sequence(
        workflow_discovery_payload.get("errors") or []
    )
    discovery_stage_timings, discovery_slowest_stages = (
        _normalise_discovery_stage_timings(
            workflow_discovery_payload.get("stage_timings")
        )
    )
    discovery_match_absence_reason = _derive_discovery_match_absence_reason(
        discovery_candidates=discovery_candidates,
        routing_matches=routing_matches,
        excluded_candidates=excluded_candidates,
        discovery_errors=discovery_errors,
        explicit_match_absence_reason=_safe_str(
            workflow_discovery_payload.get("match_absence_reason")
        ),
    )
    primary_fallback_failure_kind = None
    for attempt in selector_model_attempts:
        if attempt.get("status") == "failed":
            primary_fallback_failure_kind = _safe_str(attempt.get("failure_kind"))
            if primary_fallback_failure_kind:
                break
    if not primary_fallback_failure_kind and selector_model_errors:
        primary_fallback_failure_kind = _safe_str(
            selector_model_errors[0].get("failure_kind")
        )
    dispatch_failure_codes = _dedupe_string_sequence(
        execution_summary_payload.get("failure_codes") or []
    )
    dispatch_primary_failure_code = (
        dispatch_failure_codes[0] if dispatch_failure_codes else None
    )
    dispatch_primary_failure_reason = (
        _TOOL_EXECUTION_FAILURE_REASON_MAP.get(dispatch_primary_failure_code)
        if dispatch_primary_failure_code
        else None
    )
    raw_dispatch_terminal_unresolved_required_inputs = execution_summary_payload.get(
        "dispatch_terminal_unresolved_required_inputs"
    )
    dispatch_terminal_unresolved_required_inputs = (
        list(raw_dispatch_terminal_unresolved_required_inputs)
        if isinstance(raw_dispatch_terminal_unresolved_required_inputs, list)
        else []
    )
    tool_execution_payload_raw = execution_summary_payload.get("tool_execution")
    tool_execution_payload = (
        tool_execution_payload_raw
        if isinstance(tool_execution_payload_raw, Mapping)
        else {}
    )
    required_tool_obligations_payload_raw = execution_summary_payload.get(
        "required_tool_obligations"
    )
    if not isinstance(required_tool_obligations_payload_raw, Mapping):
        required_tool_obligations_payload_raw = tool_execution_payload.get(
            "required_tool_obligations"
        )
    required_tool_obligations_payload = (
        dict(required_tool_obligations_payload_raw)
        if isinstance(required_tool_obligations_payload_raw, Mapping)
        else {}
    )
    custom_workflow_execution_payload_raw = execution_summary_payload.get(
        "custom_workflow_execution"
    )
    custom_workflow_execution_payload = (
        custom_workflow_execution_payload_raw
        if isinstance(custom_workflow_execution_payload_raw, Mapping)
        else None
    )
    custom_workflow_terminal_effects = _normalise_workflow_execution_terminal_effects(
        custom_workflow_execution_payload.get("terminal_effects")
        if isinstance(custom_workflow_execution_payload, Mapping)
        else None
    )
    custom_workflow_side_effects = _normalise_workflow_execution_side_effects(
        custom_workflow_execution_payload.get("durable_side_effects")
        if isinstance(custom_workflow_execution_payload, Mapping)
        else None
    )
    first_dispatch_boundary = (
        _safe_str(dispatch_events[0].get("boundary")) if dispatch_events else None
    )
    last_dispatch_boundary = (
        _safe_str(dispatch_events[-1].get("boundary")) if dispatch_events else None
    )
    latest_turn_contract_check = (
        turn_contract_check_entries[-1] if turn_contract_check_entries else {}
    )
    dispatch_prepare_steps: list[dict[str, Any]] = []
    dispatch_prepare_total_duration_ms = 0
    dispatch_prepare_completed_step_count = 0
    dispatch_prepare_failed_step_count = 0
    dispatch_prepare_slowest_step_id: str | None = None
    dispatch_prepare_slowest_step_label: str | None = None
    dispatch_prepare_slowest_step_duration_ms = 0
    for event in dispatch_prepare_events:
        step_duration_ms = _safe_non_negative_int(event.get("duration_ms"))
        step_status = _safe_str(event.get("status")) or "completed"
        step_payload = {
            "step_id": _safe_str(event.get("step_id")),
            "step_label": _safe_str(event.get("step_label")),
            "status": step_status,
            "duration_ms": step_duration_ms,
            "result_summary": _safe_str(event.get("result_summary")),
            "error_class": _safe_str(event.get("error_class")),
            "error": _safe_str(event.get("error")),
            "workflow_id": _safe_str(event.get("workflow_id")),
            "workflow_name": _safe_str(event.get("workflow_name")),
            "reason_code": _safe_str(event.get("reason_code")),
            "symbol": _safe_str(event.get("symbol")),
            "workflow_launch_input_resolution_status": _safe_str(
                event.get("workflow_launch_input_resolution_status")
            ),
            "unresolved_required_inputs": _dedupe_string_sequence(
                event.get("unresolved_required_inputs") or []
            ),
        }
        dispatch_prepare_steps.append(step_payload)
        dispatch_prepare_total_duration_ms += step_duration_ms
        if step_status == "failed":
            dispatch_prepare_failed_step_count += 1
        else:
            dispatch_prepare_completed_step_count += 1
        if step_duration_ms >= dispatch_prepare_slowest_step_duration_ms:
            dispatch_prepare_slowest_step_duration_ms = step_duration_ms
            dispatch_prepare_slowest_step_id = step_payload["step_id"]
            dispatch_prepare_slowest_step_label = step_payload["step_label"]

    return {
        "schema_version": WORKFLOW_ROUTING_DIAGNOSTICS_SCHEMA_VERSION,
        "selected_workflow_id": _safe_str(workflow_routing_payload.get("workflow_id")),
        "selector_verdict": _safe_str(workflow_routing_payload.get("verdict")),
        "selector_source": _safe_str(workflow_routing_payload.get("source")),
        "turn_expected_outcome_contract": (
            dict(turn_expected_outcome_contract_payload)
            if turn_expected_outcome_contract_payload
            else None
        ),
        "turn_expected_outcome_contract_state": (
            resolved_turn_expected_outcome_contract.to_state_payload()
            if turn_expected_outcome_contract_payload
            else None
        ),
        "selection_rationale": _safe_str(
            workflow_routing_payload.get("selection_rationale")
        )
        or _safe_str(selector_entry.get("selection_rationale")),
        "discovery": {
            "query": _safe_str(workflow_discovery_payload.get("query")),
            "requested_query": _safe_str(
                workflow_discovery_payload.get("requested_query")
            ),
            "discovery_payload_origin": (
                _safe_str(workflow_discovery_payload.get("discovery_payload_origin"))
                or (
                    "missing_discovery_payload"
                    if not workflow_discovery_payload
                    else "unstamped_discovery_payload"
                )
            ),
            "discovery_payload_origin_prior": _safe_str(
                workflow_discovery_payload.get("discovery_payload_origin_prior")
            ),
            "search_time_ms": _safe_float(
                workflow_discovery_payload.get("search_time_ms")
            ),
            "timeout_budget_seconds": _safe_float(
                workflow_discovery_payload.get("timeout_budget_seconds")
            ),
            "budget_exhausted": bool(workflow_discovery_payload.get("budget_exhausted"))
            or discovery_match_absence_reason == "workflow_discovery_budget_exhausted",
            "budget_exhaustion_stage": _safe_str(
                workflow_discovery_payload.get("budget_exhaustion_stage")
            ),
            "budget_exhaustion_detail": _safe_str(
                workflow_discovery_payload.get("budget_exhaustion_detail")
            ),
            "threshold": _safe_float(workflow_discovery_payload.get("threshold")),
            "candidate_count": discovery_candidate_count,
            "match_count": discovery_match_count,
            "match_absence_reason": discovery_match_absence_reason,
            "search_sources": _dedupe_string_sequence(
                workflow_discovery_payload.get("search_sources") or []
            ),
            "errors": discovery_errors,
            "candidate_source_counts": discovery_candidate_source_counts,
            "routing_exclusion_reason_counts": discovery_exclusion_reason_counts,
            "candidate_ids": _extract_workflow_ids(
                workflow_discovery_payload.get("candidate_ids")
            )
            or [entry["concept_id"] for entry in discovery_candidates],
            "routing_match_ids": _extract_workflow_ids(
                workflow_discovery_payload.get("matches")
            )
            or [entry["concept_id"] for entry in routing_matches],
            "excluded_candidate_ids": _extract_workflow_ids(
                workflow_discovery_payload.get("excluded_candidate_ids")
            )
            or [entry["concept_id"] for entry in excluded_candidates],
            "candidates": discovery_candidates,
            "routing_matches": routing_matches,
            "excluded_candidates": excluded_candidates,
            "stage_timings": discovery_stage_timings,
            "stage_timing_count": len(discovery_stage_timings),
            "slowest_stages": discovery_slowest_stages,
        },
        "selector": {
            "prompt_id": _safe_str(workflow_routing_payload.get("prompt_id"))
            or _safe_str(_selector_context_value("prompt_id")),
            "model_name": _safe_str(selector_entry.get("model_name")),
            "confidence_score": (
                _safe_float(selector_entry.get("confidence_score"))
                if selector_entry.get("confidence_score") is not None
                else _safe_float(workflow_routing_payload.get("confidence_score"))
            ),
            "reasoning": _safe_str(selector_entry.get("reasoning"))
            or _safe_str(workflow_routing_payload.get("reasoning")),
            "prompt_failure_reason": selector_prompt_failure_reason,
            "prompt_failure_detail": selector_prompt_failure_detail,
            "requested_prompt_ids": selector_requested_prompt_ids,
            "prompt_provenance": selector_prompt_provenance,
            "discovered_workflow_ids": _extract_workflow_ids(
                _selector_context_value("discovered_workflow_ids")
            )
            or _extract_workflow_ids(
                workflow_routing_payload.get("discovered_workflow_ids")
            ),
            "candidate_count": len(selector_candidates),
            "candidate_source_counts": selector_candidate_source_counts,
            "candidate_reason_counts": selector_candidate_reason_counts,
            "candidate_entries": selector_candidates,
            "selected_candidate": selected_candidate_payload,
            "candidate_list": (
                dict(selector_candidate_list_capture)
                if isinstance(selector_candidate_list_capture, Mapping)
                else None
            ),
            "prompt": (
                dict(selector_prompt_capture)
                if isinstance(selector_prompt_capture, Mapping)
                else None
            ),
            "response": (
                dict(selector_response_capture)
                if isinstance(selector_response_capture, Mapping)
                else None
            ),
            "policy_guidance_mode": _safe_str(
                selector_entry.get("policy_guidance_mode")
            ),
            "policy_snapshot_id": _safe_str(selector_entry.get("policy_snapshot_id")),
            "policy_candidate_scores": (
                list(raw_policy_candidate_scores)
                if isinstance(raw_policy_candidate_scores, list)
                else None
            ),
            "model_request": selector_model_request,
            "context_summary": selector_context_summary,
            "context_lineage": selector_context_lineage,
            "selected_model_candidate": selected_model_candidate,
            "fallback_used": bool(selector_model_policy_entry.get("fallback_used")),
            "fallback_attempt_count": _safe_non_negative_int(
                selector_model_policy_entry.get("fallback_attempt_count")
            ),
            "model_failure_count": _safe_non_negative_int(
                selector_model_policy_entry.get("failure_count")
            ),
            "primary_fallback_failure_kind": primary_fallback_failure_kind,
            "fallback_failure_kind_counts": selector_fallback_failure_kind_counts,
            "model_attempts": selector_model_attempts,
            "model_errors": selector_model_errors,
            "selection_resolution": _safe_str(
                selector_selection_metadata.get("selection_resolution")
            ),
            "raw_candidate_label": _safe_str(
                selector_selection_metadata.get("raw_candidate_label")
            ),
            "raw_response_format": _safe_str(
                selector_selection_metadata.get("raw_response_format")
            ),
            "selection_metadata": selector_selection_metadata or None,
            "override_events": selector_override_entries,
            "telemetry_completeness": {
                "prompt_present": selector_prompt_present,
                "candidate_list_present": selector_candidate_list_present,
                "response_present": selector_response_present,
                "candidate_entries_present": bool(selector_candidates),
                "context_lineage_present": isinstance(
                    selector_context_lineage, Mapping
                ),
                "selector_prompt_entry_count": len(selector_prompt_entries),
                "selector_response_entry_count": len(selector_entries),
                "missing_fields": selector_missing_telemetry_fields,
                "response_absence_reason": selector_response_absence_reason,
            },
        },
        "dispatch": {
            "selected_execution_mode": _safe_str(
                execution_summary_payload.get("selected_execution_mode")
            ),
            "dispatch_workflow_id": _safe_str(
                execution_summary_payload.get("dispatch_workflow_id")
            ),
            "dispatch_event_count": _safe_non_negative_int(
                execution_summary_payload.get("dispatch_event_count")
            ),
            "contract_resolution_status": _safe_str(
                execution_summary_payload.get("contract_resolution_status")
            ),
            "workflow_handoff_started": bool(
                execution_summary_payload.get("workflow_handoff_started")
            ),
            "workflow_handoff_failure_reason": _safe_str(
                execution_summary_payload.get("workflow_handoff_failure_reason")
            ),
            "workflow_handoff_failure_error_class": _safe_str(
                execution_summary_payload.get("workflow_handoff_failure_error_class")
            ),
            "dispatch_terminal_status": _safe_str(
                execution_summary_payload.get("dispatch_terminal_status")
            ),
            "dispatch_terminal_final_state": _safe_str(
                execution_summary_payload.get("dispatch_terminal_final_state")
            ),
            "dispatch_terminal_completed": (
                execution_summary_payload.get("dispatch_terminal_completed")
                if isinstance(
                    execution_summary_payload.get("dispatch_terminal_completed"),
                    bool,
                )
                else None
            ),
            "dispatch_terminal_failure_reason": _safe_str(
                execution_summary_payload.get("dispatch_terminal_failure_reason")
            ),
            "dispatch_terminal_failure_error_class": _safe_str(
                execution_summary_payload.get("dispatch_terminal_failure_error_class")
            ),
            "dispatch_terminal_failure_detail": _safe_str(
                execution_summary_payload.get("dispatch_terminal_failure_detail")
            ),
            "dispatch_terminal_failing_state_id": _safe_str(
                execution_summary_payload.get("dispatch_terminal_failing_state_id")
            ),
            "dispatch_terminal_failing_action_id": _safe_str(
                execution_summary_payload.get("dispatch_terminal_failing_action_id")
            ),
            "dispatch_terminal_unresolved_required_inputs": (
                dispatch_terminal_unresolved_required_inputs
            ),
            "dispatch_terminal_launch_input_resolution_status": _safe_str(
                execution_summary_payload.get(
                    "dispatch_terminal_launch_input_resolution_status"
                )
            ),
            "tool_route_selected": bool(
                execution_summary_payload.get("tool_route_selected")
            ),
            "planned_count": _safe_non_negative_int(
                execution_summary_payload.get("planned_count")
            ),
            "started_count": _safe_non_negative_int(
                execution_summary_payload.get("started_count")
            ),
            "executed_count": _safe_non_negative_int(
                execution_summary_payload.get("executed_count")
            ),
            "successful_invocation_count": _safe_non_negative_int(
                execution_summary_payload.get("successful_invocation_count")
            ),
            "failed_invocation_count": _safe_non_negative_int(
                execution_summary_payload.get("failed_invocation_count")
            ),
            "blocked_invocation_count": _safe_non_negative_int(
                execution_summary_payload.get("blocked_invocation_count")
            ),
            "zero_tools_executed": bool(
                execution_summary_payload.get("zero_tools_executed")
            ),
            "zero_tool_reason_code": _safe_str(
                execution_summary_payload.get("zero_tool_reason_code")
            ),
            "zero_tool_reason": _safe_str(
                execution_summary_payload.get("zero_tool_reason")
            ),
            "zero_tool_execution_expected": (
                execution_summary_payload.get("zero_tool_execution_expected")
                if isinstance(
                    execution_summary_payload.get("zero_tool_execution_expected"),
                    bool,
                )
                else None
            ),
            "required_effects_required_tools": _dedupe_string_sequence(
                execution_summary_payload.get("required_effects_required_tools") or []
            ),
            "required_effects_required_tool_count": _safe_non_negative_int(
                execution_summary_payload.get("required_effects_required_tool_count")
            ),
            "required_effects_missing_required_tools": _dedupe_string_sequence(
                execution_summary_payload.get("required_effects_missing_required_tools")
                or []
            ),
            "required_effects_missing_required_tool_count": _safe_non_negative_int(
                execution_summary_payload.get(
                    "required_effects_missing_required_tool_count"
                )
            ),
            "required_effects_unresolved_effect_ids": _dedupe_string_sequence(
                execution_summary_payload.get("required_effects_unresolved_effect_ids")
                or []
            ),
            "required_effects_unresolved_effect_types": _dedupe_string_sequence(
                execution_summary_payload.get(
                    "required_effects_unresolved_effect_types"
                )
                or []
            ),
            "workflow_required_effects_contract_id": _safe_str(
                execution_summary_payload.get("workflow_required_effects_contract_id")
            ),
            "workflow_required_effects_declared_count": _safe_non_negative_int(
                execution_summary_payload.get(
                    "workflow_required_effects_declared_count"
                )
            ),
            "workflow_required_effects_contract_source": _safe_str(
                execution_summary_payload.get(
                    "workflow_required_effects_contract_source"
                )
            ),
            "workflow_required_effects_materialised_count": _safe_non_negative_int(
                execution_summary_payload.get(
                    "workflow_required_effects_materialised_count"
                )
            ),
            "required_tool_obligations": required_tool_obligations_payload,
            "required_tool_obligation_unsatisfied_count": _safe_non_negative_int(
                execution_summary_payload.get(
                    "required_tool_obligation_unsatisfied_count"
                )
            ),
            "required_tool_obligation_blocking_failure_codes": (
                _dedupe_string_sequence(
                    execution_summary_payload.get(
                        "required_tool_obligation_blocking_failure_codes"
                    )
                    or []
                )
            ),
            "failure_codes": dispatch_failure_codes,
            "zero_execution_primary_failure_code": dispatch_primary_failure_code,
            "zero_execution_primary_failure_reason": dispatch_primary_failure_reason,
            "pre_dispatch": {
                "step_count": len(dispatch_prepare_steps),
                "completed_step_count": dispatch_prepare_completed_step_count,
                "failed_step_count": dispatch_prepare_failed_step_count,
                "total_duration_ms": dispatch_prepare_total_duration_ms,
                "slowest_step_id": dispatch_prepare_slowest_step_id,
                "slowest_step_label": dispatch_prepare_slowest_step_label,
                "slowest_step_duration_ms": dispatch_prepare_slowest_step_duration_ms,
                "steps": dispatch_prepare_steps,
            },
            "turn_contract_check": {
                "status": _safe_str(latest_turn_contract_check.get("status")),
                "selected_workflow_id": _safe_str(
                    latest_turn_contract_check.get("selected_workflow_id")
                ),
                "selected_workflow_can_satisfy_contract": (
                    latest_turn_contract_check.get(
                        "selected_workflow_can_satisfy_contract"
                    )
                    if isinstance(
                        latest_turn_contract_check.get(
                            "selected_workflow_can_satisfy_contract"
                        ),
                        bool,
                    )
                    else None
                ),
                "required_tools": _dedupe_string_sequence(
                    latest_turn_contract_check.get("required_tools") or []
                ),
                "required_surface_families": _dedupe_string_sequence(
                    latest_turn_contract_check.get("required_surface_families") or []
                ),
                "external_surface_families": _dedupe_string_sequence(
                    latest_turn_contract_check.get("external_surface_families") or []
                ),
                "override_reason": _safe_str(
                    latest_turn_contract_check.get("override_reason")
                ),
                "reasoning": _safe_str(latest_turn_contract_check.get("reasoning")),
                "turn_expected_outcome_contract": (
                    dict(
                        cast(
                            Mapping[str, Any],
                            latest_turn_contract_check.get(
                                "turn_expected_outcome_contract"
                            ),
                        )
                    )
                    if isinstance(
                        latest_turn_contract_check.get(
                            "turn_expected_outcome_contract"
                        ),
                        Mapping,
                    )
                    else None
                ),
            },
            "tool_execution": {
                "planned_count": _safe_non_negative_int(
                    tool_execution_payload.get("planned_count")
                ),
                "started_count": _safe_non_negative_int(
                    tool_execution_payload.get("started_count")
                ),
                "executed_count": _safe_non_negative_int(
                    tool_execution_payload.get("executed_count")
                ),
                "invocation_count": _safe_non_negative_int(
                    tool_execution_payload.get("invocation_count")
                ),
                "successful_invocation_count": _safe_non_negative_int(
                    tool_execution_payload.get("successful_invocation_count")
                ),
                "failed_invocation_count": _safe_non_negative_int(
                    tool_execution_payload.get("failed_invocation_count")
                ),
                "blocked_invocation_count": _safe_non_negative_int(
                    tool_execution_payload.get("blocked_invocation_count")
                ),
                "worker_unavailable_event_count": _safe_non_negative_int(
                    tool_execution_payload.get("worker_unavailable_event_count")
                ),
                "tool_plan_stage_event_count": _safe_non_negative_int(
                    tool_execution_payload.get("tool_plan_stage_event_count")
                ),
                "tool_execute_stage_event_count": _safe_non_negative_int(
                    tool_execution_payload.get("tool_execute_stage_event_count")
                ),
                "parse_error_invocation_count": _safe_non_negative_int(
                    tool_execution_payload.get("parse_error_invocation_count")
                ),
                "validation_error_invocation_count": _safe_non_negative_int(
                    tool_execution_payload.get("validation_error_invocation_count")
                ),
                "zero_tools_executed": bool(
                    tool_execution_payload.get("zero_tools_executed")
                ),
                "failure_codes": _dedupe_string_sequence(
                    tool_execution_payload.get("failure_codes") or []
                ),
                "required_tool_obligations": required_tool_obligations_payload,
                "required_tool_obligation_unsatisfied_count": (
                    _safe_non_negative_int(
                        tool_execution_payload.get(
                            "required_tool_obligation_unsatisfied_count"
                        )
                    )
                ),
            },
            "custom_workflow_execution": (
                {
                    "observed": bool(custom_workflow_execution_payload.get("observed")),
                    "schema_version": _safe_str(
                        custom_workflow_execution_payload.get("schema_version")
                    ),
                    "workflow_id": _safe_str(
                        custom_workflow_execution_payload.get("workflow_id")
                    ),
                    "completed": (
                        custom_workflow_execution_payload.get("completed")
                        if isinstance(
                            custom_workflow_execution_payload.get("completed"), bool
                        )
                        else None
                    ),
                    "effective_completed": (
                        custom_workflow_execution_payload.get("effective_completed")
                        if isinstance(
                            custom_workflow_execution_payload.get(
                                "effective_completed"
                            ),
                            bool,
                        )
                        else None
                    ),
                    "terminal_status": _safe_str(
                        custom_workflow_execution_payload.get("terminal_status")
                    ),
                    "final_state": _safe_str(
                        custom_workflow_execution_payload.get("final_state")
                    ),
                    "workflow_instance_id": _safe_str(
                        custom_workflow_execution_payload.get("workflow_instance_id")
                    ),
                    "workflow_instance_status": _safe_str(
                        custom_workflow_execution_payload.get(
                            "workflow_instance_status"
                        )
                    ),
                    "workflow_instance_retry_count": (
                        _safe_non_negative_int(
                            custom_workflow_execution_payload.get(
                                "workflow_instance_retry_count"
                            )
                        )
                        if custom_workflow_execution_payload.get(
                            "workflow_instance_retry_count"
                        )
                        is not None
                        else None
                    ),
                    "workflow_instance_max_retries": (
                        _safe_non_negative_int(
                            custom_workflow_execution_payload.get(
                                "workflow_instance_max_retries"
                            )
                        )
                        if custom_workflow_execution_payload.get(
                            "workflow_instance_max_retries"
                        )
                        is not None
                        else None
                    ),
                    "completion_gate_safe_to_claim_completion": (
                        custom_workflow_execution_payload.get(
                            "completion_gate_safe_to_claim_completion"
                        )
                        if isinstance(
                            custom_workflow_execution_payload.get(
                                "completion_gate_safe_to_claim_completion"
                            ),
                            bool,
                        )
                        else None
                    ),
                    "completion_gate_blocking_reason_codes": _dedupe_string_sequence(
                        custom_workflow_execution_payload.get(
                            "completion_gate_blocking_reason_codes"
                        )
                        or []
                    ),
                    "terminal_success_contract": (
                        dict(
                            cast(
                                Mapping[str, Any],
                                custom_workflow_execution_payload.get(
                                    "terminal_success_contract"
                                ),
                            )
                        )
                        if isinstance(
                            custom_workflow_execution_payload.get(
                                "terminal_success_contract"
                            ),
                            Mapping,
                        )
                        else None
                    ),
                    "terminal_success_evaluation": (
                        dict(
                            cast(
                                Mapping[str, Any],
                                custom_workflow_execution_payload.get(
                                    "terminal_success_evaluation"
                                ),
                            )
                        )
                        if isinstance(
                            custom_workflow_execution_payload.get(
                                "terminal_success_evaluation"
                            ),
                            Mapping,
                        )
                        else None
                    ),
                    "step_result_envelope_count": _safe_non_negative_int(
                        custom_workflow_execution_payload.get(
                            "step_result_envelope_count"
                        )
                    ),
                    "action_started_count": _safe_non_negative_int(
                        custom_workflow_execution_payload.get("action_started_count")
                    ),
                    "action_completed_count": _safe_non_negative_int(
                        custom_workflow_execution_payload.get("action_completed_count")
                    ),
                    "action_success_count": _safe_non_negative_int(
                        custom_workflow_execution_payload.get("action_success_count")
                    ),
                    "action_failure_count": _safe_non_negative_int(
                        custom_workflow_execution_payload.get("action_failure_count")
                    ),
                    "action_unknown_count": _safe_non_negative_int(
                        custom_workflow_execution_payload.get("action_unknown_count")
                    ),
                    "first_failing_state_id": _safe_str(
                        custom_workflow_execution_payload.get("first_failing_state_id")
                    ),
                    "first_failing_action_id": _safe_str(
                        custom_workflow_execution_payload.get("first_failing_action_id")
                    ),
                    "runtime_event_count": _safe_non_negative_int(
                        custom_workflow_execution_payload.get("runtime_event_count")
                    ),
                    "terminal_effect_count": _safe_non_negative_int(
                        custom_workflow_execution_payload.get("terminal_effect_count")
                    ),
                    "terminal_effects": custom_workflow_terminal_effects,
                    "durable_side_effect_count": _safe_non_negative_int(
                        custom_workflow_execution_payload.get(
                            "durable_side_effect_count"
                        )
                    ),
                    "durable_side_effects": custom_workflow_side_effects,
                }
                if isinstance(custom_workflow_execution_payload, Mapping)
                else None
            ),
            "last_successful_boundary": _safe_str(
                execution_summary_payload.get("last_successful_boundary")
            ),
            "first_post_selection_boundary": first_dispatch_boundary,
            "latest_boundary": last_dispatch_boundary,
            "events": dispatch_events,
        },
    }


def _extract_completion_claim_signals(
    *,
    response_text: Any,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    del response_text
    detected_count = 0
    validation_seen = False
    verified_count = 0
    not_verified_count = 0

    for entry in aux_llm_calls or ():
        if not isinstance(entry, Mapping):
            continue
        entry_type = _safe_str(entry.get("type"))
        if entry_type == "completion_claim_detection":
            try:
                detected_count = max(detected_count, int(entry.get("claim_count") or 0))
            except Exception:
                continue
        if entry_type == "completion_claim_validation":
            validation_seen = True
            try:
                verified_count = max(
                    verified_count, int(entry.get("verified_count") or 0)
                )
            except Exception:
                pass
            try:
                not_verified_count = max(
                    not_verified_count, int(entry.get("not_verified_count") or 0)
                )
            except Exception:
                pass

    detected = detected_count > 0
    if not detected:
        validated = True
    elif validation_seen:
        validated = not_verified_count == 0
    else:
        validated = False

    return {
        "detected": detected,
        "detected_count": detected_count,
        "validation_seen": validation_seen,
        "verified_count": verified_count,
        "not_verified_count": not_verified_count,
        "validated": validated,
    }


def _extract_tool_invocation_payload(
    invocation: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    payload_value = invocation.get("effective_payload")
    if isinstance(payload_value, Mapping):
        return payload_value
    payload_value = invocation.get("payload")
    if isinstance(payload_value, Mapping):
        return payload_value
    payload_value = invocation.get("effective_arguments")
    if isinstance(payload_value, Mapping):
        return payload_value
    payload_value = invocation.get("arguments")
    if isinstance(payload_value, Mapping):
        return payload_value
    return None


def _classify_tool_invocation_status(
    *,
    invocation: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
) -> str:
    payload_value = (
        payload
        if isinstance(payload, Mapping)
        else _extract_tool_invocation_payload(invocation)
    )
    error_value = _safe_str(invocation.get("error"))
    blocked = bool(invocation.get("blocked"))
    payload_status = ""
    payload_success: bool | None = None
    if isinstance(payload_value, Mapping):
        payload_status = (_safe_str(payload_value.get("status")) or "").lower()
        raw_success = payload_value.get("success")
        if isinstance(raw_success, bool):
            payload_success = raw_success

    if blocked:
        return "blocked"
    invocation_status = (_safe_str(invocation.get("status")) or "").lower()
    error_code = (_safe_str(invocation.get("error_code")) or "").lower()
    if (
        invocation_status in {"timeout", "timed_out"}
        or payload_status in {"timeout", "timed_out"}
        or error_code in {"tool_timeout", "tool_timeout_outcome_unknown"}
    ):
        return "timeout"
    if (
        error_value
        or payload_status in {"error", "failed", "failure"}
        or payload_success is False
    ):
        return "error"
    return "ok"


def _summarise_tool_invocations(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> tuple[
    list[dict[str, Any]],
    list[str],
    list[str],
    list[str],
    list[str],
    list[str],
]:
    serialised: list[dict[str, Any]] = []
    successful_tools: list[str] = []
    failed_tools: list[str] = []
    blocked_tools: list[str] = []
    successful_write_tools: list[str] = []
    successful_verification_tools: list[str] = []

    for invocation in tool_invocations or ():
        if not isinstance(invocation, Mapping):
            continue

        tool_name = _safe_str(invocation.get("tool")) or _safe_str(
            invocation.get("method")
        )
        if not tool_name:
            continue

        payload_value = invocation.get("effective_payload")
        if not isinstance(payload_value, Mapping):
            payload_value = invocation.get("payload")

        status = _classify_tool_invocation_status(
            invocation=invocation, payload=payload_value
        )
        error_value = _safe_str(invocation.get("error"))

        result_summary = _safe_str(invocation.get("result_summary"))
        if not result_summary and isinstance(payload_value, Mapping):
            result_summary = _safe_str(
                payload_value.get("result_summary")
            ) or _safe_str(payload_value.get("summary"))
        target_ids = _extract_tool_invocation_target_ids(invocation)

        serialised_invocation = {
            "tool": tool_name,
            "status": status,
            "started_at_utc": _safe_str(invocation.get("started_at_utc")),
            "completed_at_utc": _safe_str(invocation.get("completed_at_utc")),
            "error": error_value,
            "error_code": _safe_str(invocation.get("error_code")),
            "result_summary": result_summary,
            "payload_fingerprint": _hash_payload(payload_value),
        }
        for timing_key in (
            "duration_ms",
            "queue_duration_ms",
            "handler_duration_ms",
            "handler_elapsed_ms",
            "transport_overhead_ms",
            "timeout_sec",
            "advisory_timeout_sec",
        ):
            timing_value = _safe_float(invocation.get(timing_key))
            if timing_value is not None:
                serialised_invocation[timing_key] = timing_value
        for identifier_key in (
            "call_id",
            "execution_id",
            "timeout_phase",
            "effect_id",
            "effect_status",
        ):
            identifier_value = _safe_str(invocation.get(identifier_key))
            if identifier_value:
                serialised_invocation[identifier_key] = identifier_value
        if isinstance(invocation.get("changed"), bool):
            serialised_invocation["changed"] = invocation.get("changed")
        if isinstance(invocation.get("advisory_budget_exceeded"), bool):
            serialised_invocation["advisory_budget_exceeded"] = invocation.get(
                "advisory_budget_exceeded"
            )
        transport_metadata = invocation.get("transport")
        if isinstance(transport_metadata, Mapping):
            serialised_invocation["transport"] = {
                key: transport_metadata.get(key)
                for key in (
                    "schema_version",
                    "execution_id",
                    "outcome",
                    "duration_ms",
                    "timeout_sec",
                    "advisory_timeout_sec",
                    "advisory_budget_exceeded",
                    "queue_duration_ms",
                    "handler_duration_ms",
                    "handler_elapsed_ms",
                    "transport_overhead_ms",
                    "timeout_phase",
                    "late_result_policy",
                )
                if key in transport_metadata
            }
        if target_ids:
            serialised_invocation["target_ids"] = target_ids
        serialised.append(serialised_invocation)

        lowered = tool_name.lower()
        if status == "ok":
            if lowered not in {name.lower() for name in successful_tools}:
                successful_tools.append(tool_name)
            if _is_write_tool(tool_name) and lowered not in {
                name.lower() for name in successful_write_tools
            }:
                successful_write_tools.append(tool_name)
            if _is_verification_read_tool(tool_name) and lowered not in {
                name.lower() for name in successful_verification_tools
            }:
                successful_verification_tools.append(tool_name)
        elif status == "blocked":
            if lowered not in {name.lower() for name in blocked_tools}:
                blocked_tools.append(tool_name)
        else:
            if lowered not in {name.lower() for name in failed_tools}:
                failed_tools.append(tool_name)

    return (
        serialised,
        successful_tools,
        failed_tools,
        blocked_tools,
        successful_write_tools,
        successful_verification_tools,
    )


def _summarise_tool_execution_context(
    *,
    workflow_routing: Mapping[str, Any] | None,
    turn_execution_diagnostics: Mapping[str, Any] | None,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    serialised_invocations: Sequence[Mapping[str, Any]],
    selected_workflow_trace: Mapping[str, Any] | None = None,
    workflow_failure_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_workflow_id = (
        _safe_str(workflow_routing.get("workflow_id"))
        if isinstance(workflow_routing, Mapping)
        else None
    )
    selector_verdict = (
        _safe_str(workflow_routing.get("verdict"))
        if isinstance(workflow_routing, Mapping)
        else None
    )
    selector_verdict_lower = (selector_verdict or "").lower()
    selected_workflow_id_lower = (selected_workflow_id or "").lower()

    tool_route_selected = bool(
        selector_verdict_lower in _TOOL_CALLING_SELECTOR_VERDICTS
        or "tool_calling_workflow" in selected_workflow_id_lower
    )
    plain_response_route_selected = bool(
        (selected_workflow_id or "") in _PLAIN_RESPONSE_WORKFLOW_IDS
        or selector_verdict_lower == "plain_response"
    )
    custom_workflow_route_selected = bool(
        selected_workflow_id
        and not tool_route_selected
        and not plain_response_route_selected
    )

    latest_progress = (
        turn_execution_diagnostics.get("latest_progress")
        if isinstance(turn_execution_diagnostics, Mapping)
        else None
    )
    counters = (
        latest_progress.get("counters")
        if isinstance(latest_progress, Mapping)
        else None
    )
    progress_tools_started = (
        _safe_non_negative_int(counters.get("tools_started"))
        if isinstance(counters, Mapping)
        else 0
    )
    progress_tools_completed = (
        _safe_non_negative_int(counters.get("tools_completed"))
        if isinstance(counters, Mapping)
        else 0
    )

    diagnostic_events = []
    if isinstance(latest_progress, Mapping):
        raw_events = latest_progress.get("diagnostic_events")
        if isinstance(raw_events, list):
            diagnostic_events = [
                event for event in raw_events if isinstance(event, Mapping)
            ]

    tool_observation_summary = derive_tool_observations_from_diagnostic_events(
        diagnostic_events
    )
    tool_call_start_event_count = _safe_non_negative_int(
        tool_observation_summary.get("tool_call_start_count")
    )
    tool_call_end_event_count = _safe_non_negative_int(
        tool_observation_summary.get("tool_call_end_count")
    )

    worker_unavailable_event_count = 0
    tool_plan_stage_event_count = 0
    tool_execute_stage_event_count = 0
    for event in diagnostic_events:
        liveness_reason = (_safe_str(event.get("liveness_reason")) or "").lower()
        if liveness_reason == "worker_unavailable":
            worker_unavailable_event_count += 1
        stage = (_safe_str(event.get("stage")) or "").lower()
        phase = (_safe_str(event.get("phase")) or "").lower()
        if "tool_plan" in {stage, phase}:
            tool_plan_stage_event_count += 1
        if "tool_execute" in {stage, phase}:
            tool_execute_stage_event_count += 1

    parse_error_invocation_count = 0
    validation_error_invocation_count = 0
    successful_invocation_count = 0
    failed_invocation_count = 0
    blocked_invocation_count = 0
    for invocation in serialised_invocations:
        if not isinstance(invocation, Mapping):
            continue
        tool_name = (_safe_str(invocation.get("tool")) or "").lower()
        invocation_status = (_safe_str(invocation.get("status")) or "").lower()
        if invocation_status == "ok":
            successful_invocation_count += 1
        elif invocation_status == "blocked":
            blocked_invocation_count += 1
        elif invocation_status:
            failed_invocation_count += 1
        if tool_name == "__tool_call_parse_error__":
            parse_error_invocation_count += 1
        elif tool_name == "__tool_call_validation_error__":
            validation_error_invocation_count += 1

    missing_tool_parse_error_count = 0
    missing_tool_retry_exhausted_count = 0
    missing_tool_unresolved_count = 0
    dispatch_boundary_events = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_dispatch_boundary",
    )
    workflow_instance_submission_events = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_instance_submission",
    )
    workflow_use_episode_events = _collect_aux_entries(
        aux_llm_calls,
        entry_type="workflow_use_episode",
    )
    selected_execution_mode = ""
    dispatch_workflow_id = ""
    contract_resolution_status = ""
    execution_mode_selected_explicitly = False
    workflow_handoff_started = False
    workflow_handoff_failure_reason = ""
    workflow_handoff_failure_error_class = ""
    dispatch_terminal_status = ""
    dispatch_terminal_final_state = ""
    dispatch_terminal_completed: bool | None = None
    dispatch_terminal_failure_reason = ""
    dispatch_terminal_failure_error_class = ""
    dispatch_terminal_failure_detail = ""
    dispatch_terminal_failing_state_id = ""
    dispatch_terminal_failing_action_id = ""
    dispatch_terminal_unresolved_required_inputs: list[str] = []
    dispatch_terminal_launch_input_resolution_status = ""
    # JVNAUTOSCI-1809: Prefer the submission event matching the selector's
    # chosen workflow.  Post-processing workflows (e.g. buttonify) append
    # later submission events; using [-1] would misattribute dispatch
    # identity to the post-processing workflow instead of the primary one.
    latest_workflow_instance_submission: Mapping[str, Any] = {}
    if workflow_instance_submission_events:
        if selected_workflow_id:
            for _sub_evt in workflow_instance_submission_events:
                if _safe_str(_sub_evt.get("workflow_id")) == selected_workflow_id:
                    latest_workflow_instance_submission = _sub_evt
                    break
        if not latest_workflow_instance_submission:
            latest_workflow_instance_submission = workflow_instance_submission_events[0]
    latest_workflow_use_episode: Mapping[str, Any] = {}
    if workflow_use_episode_events:
        if selected_workflow_id:
            for _episode_evt in workflow_use_episode_events:
                if _safe_str(_episode_evt.get("workflow_id")) == selected_workflow_id:
                    latest_workflow_use_episode = _episode_evt
                    break
        if not latest_workflow_use_episode:
            latest_workflow_use_episode = workflow_use_episode_events[0]
    for entry in aux_llm_calls or ():
        if not isinstance(entry, Mapping):
            continue
        entry_type = (_safe_str(entry.get("type")) or "").lower()
        if entry_type == "missing_tool_call_detection":
            parse_error = _safe_str(entry.get("parse_error"))
            retry_reason = _safe_str(entry.get("retry_reason"))
            if parse_error:
                missing_tool_parse_error_count += 1
            if retry_reason:
                missing_tool_unresolved_count += 1
        elif entry_type == "missing_tool_call_retry":
            retry_reason = _safe_str(entry.get("retry_reason"))
            if not retry_reason:
                continue
            stage = (_safe_str(entry.get("stage")) or "").lower()
            retries_remaining = _safe_non_negative_int(entry.get("retries_remaining"))
            if stage == "skipped" or retries_remaining <= 0:
                missing_tool_retry_exhausted_count += 1

    for event in dispatch_boundary_events:
        boundary = (_safe_str(event.get("boundary")) or "").lower()
        status = (_safe_str(event.get("status")) or "").lower()
        candidate_execution_mode = _safe_str(event.get("selected_execution_mode"))
        if candidate_execution_mode:
            selected_execution_mode = candidate_execution_mode
        if boundary == "execution_mode_selected" and status == "selected":
            execution_mode_selected_explicitly = True
        candidate_dispatch_workflow_id = _safe_str(event.get("dispatch_workflow_id"))
        if candidate_dispatch_workflow_id:
            dispatch_workflow_id = candidate_dispatch_workflow_id
        if boundary == "contract_resolution" and status:
            contract_resolution_status = status
        elif boundary == "workflow_handoff" and status == "started":
            workflow_handoff_started = True
        elif boundary == "workflow_handoff" and status == "failed":
            workflow_handoff_failure_reason = _safe_str(event.get("reason")) or ""
            workflow_handoff_failure_error_class = (
                _safe_str(event.get("error_class")) or ""
            )
        elif boundary == "workflow_terminal":
            if status:
                dispatch_terminal_status = status
            final_state_value = _safe_str(event.get("final_state"))
            if final_state_value:
                dispatch_terminal_final_state = final_state_value
            completed_value = event.get("completed")
            if isinstance(completed_value, bool):
                dispatch_terminal_completed = completed_value
            failure_detail_value = _safe_str(event.get("detail")) or _safe_str(
                event.get("error")
            )
            if failure_detail_value:
                dispatch_terminal_failure_detail = failure_detail_value
            failing_state_id = _safe_str(event.get("failing_state_id"))
            if failing_state_id:
                dispatch_terminal_failing_state_id = failing_state_id
            failing_action_id = _safe_str(event.get("failing_action_id"))
            if failing_action_id:
                dispatch_terminal_failing_action_id = failing_action_id
            launch_input_resolution_status = _safe_str(
                event.get("workflow_launch_input_resolution_status")
            )
            if launch_input_resolution_status:
                dispatch_terminal_launch_input_resolution_status = (
                    launch_input_resolution_status
                )
            unresolved_required_inputs = event.get("unresolved_required_inputs")
            if isinstance(unresolved_required_inputs, list):
                dispatch_terminal_unresolved_required_inputs = _dedupe_string_sequence(
                    unresolved_required_inputs
                )
            if status == "failed":
                dispatch_terminal_failure_reason = _safe_str(event.get("reason")) or ""
                dispatch_terminal_failure_error_class = (
                    _safe_str(event.get("error_class")) or ""
                )

    submission_status = (
        _safe_str(latest_workflow_instance_submission.get("status")) or ""
    )
    submission_reason_code = (
        _safe_str(latest_workflow_instance_submission.get("reason_code")) or ""
    )
    submission_payload = latest_workflow_instance_submission.get("submission")
    submission_payload = (
        submission_payload if isinstance(submission_payload, Mapping) else {}
    )
    submission_verification = submission_payload.get("verification")
    submission_verification = (
        submission_verification if isinstance(submission_verification, Mapping) else {}
    )
    submission_verification_failed = (
        submission_verification.get("runnable_verification_success") is False
    )
    if (
        not dispatch_boundary_events
        and submission_status == "submission_failed"
        and (
            submission_verification_failed
            or submission_reason_code == "workflow_not_runnable"
        )
    ):
        dispatch_workflow_id = (
            _safe_str(latest_workflow_instance_submission.get("workflow_id"))
            or dispatch_workflow_id
        )
        if not selected_execution_mode:
            if tool_route_selected:
                selected_execution_mode = "tool_pipeline"
            elif plain_response_route_selected:
                selected_execution_mode = "direct_response"
            else:
                selected_execution_mode = "custom_workflow"
        dispatch_terminal_status = "failed"
        dispatch_terminal_failure_reason = (
            submission_reason_code or "workflow_instance_submission_failed"
        )
        dispatch_terminal_failure_detail = (
            _safe_str(latest_workflow_instance_submission.get("error"))
            or _safe_str(submission_payload.get("error"))
            or dispatch_terminal_failure_detail
        )
    if not dispatch_terminal_status and latest_workflow_use_episode:
        episode_completed = latest_workflow_use_episode.get("completed")
        termination_reason = latest_workflow_use_episode.get("termination_reason")
        termination_reason = (
            termination_reason if isinstance(termination_reason, Mapping) else {}
        )
        episode_terminal_stage = (
            _safe_str(latest_workflow_use_episode.get("terminal_stage")) or ""
        )
        episode_failure_code = _safe_str(termination_reason.get("code")) or ""
        episode_failure_detail = _safe_str(termination_reason.get("detail")) or ""
        workflow_access_failure_codes = {
            "workflow_not_registered",
            "workflow_definition_not_found",
            "workflow_lookup_failed",
            "workflow_access_failure",
        }
        if episode_completed is False and (
            episode_terminal_stage == "workflow_lookup"
            or episode_failure_code in workflow_access_failure_codes
            or episode_failure_detail in workflow_access_failure_codes
        ):
            dispatch_workflow_id = (
                _safe_str(latest_workflow_use_episode.get("workflow_id"))
                or dispatch_workflow_id
            )
            if not selected_execution_mode:
                selected_execution_mode = "custom_workflow"
            dispatch_terminal_status = "failed"
            dispatch_terminal_completed = False
            dispatch_terminal_final_state = (
                _safe_str(latest_workflow_use_episode.get("final_state"))
                or dispatch_terminal_final_state
            )
            dispatch_terminal_failure_reason = "workflow_access_failure"
            dispatch_terminal_failure_detail = (
                episode_failure_detail
                or episode_failure_code
                or dispatch_terminal_failure_detail
            )

    invocation_count = len(serialised_invocations)
    observed_started_count = max(progress_tools_started, tool_call_start_event_count)
    observed_executed_count = max(
        progress_tools_completed,
        tool_call_end_event_count,
        invocation_count,
    )
    if not selected_execution_mode:
        if tool_route_selected:
            selected_execution_mode = "tool_pipeline"
        elif custom_workflow_route_selected:
            selected_execution_mode = "custom_workflow"
        elif plain_response_route_selected:
            selected_execution_mode = "direct_response"
    if not dispatch_workflow_id and selected_execution_mode in {
        "direct_response",
        "custom_workflow",
        "tool_pipeline",
    }:
        dispatch_workflow_id = selected_workflow_id or ""
    custom_workflow_execution = _build_custom_workflow_execution_summary(
        aux_llm_calls=aux_llm_calls,
        selected_execution_mode=selected_execution_mode,
        selected_workflow_id=selected_workflow_id or "",
        dispatch_workflow_id=dispatch_workflow_id,
        dispatch_terminal_completed=dispatch_terminal_completed,
        dispatch_terminal_final_state=dispatch_terminal_final_state,
        dispatch_terminal_failing_state_id=dispatch_terminal_failing_state_id,
        dispatch_terminal_failing_action_id=dispatch_terminal_failing_action_id,
        selected_workflow_trace=selected_workflow_trace,
    )
    if isinstance(custom_workflow_execution, Mapping):
        fallback_terminal_status = _safe_str(
            custom_workflow_execution.get("terminal_status")
        )
        if fallback_terminal_status and not dispatch_terminal_status:
            dispatch_terminal_status = fallback_terminal_status
        fallback_final_state = _safe_str(custom_workflow_execution.get("final_state"))
        if fallback_final_state and not dispatch_terminal_final_state:
            dispatch_terminal_final_state = fallback_final_state
        fallback_error = _safe_str(custom_workflow_execution.get("error"))
        if fallback_error and not dispatch_terminal_failure_detail:
            dispatch_terminal_failure_detail = fallback_error
        if (
            fallback_error
            and (dispatch_terminal_status or "").lower() == "failed"
            and not dispatch_terminal_failure_reason
        ):
            dispatch_terminal_failure_reason = "child_workflow_failed"

    failure_codes: list[str] = []
    critical_failure_codes, critical_failure_detail = (
        _extract_critical_workflow_failure_evidence(workflow_failure_evidence)
    )
    failure_codes.extend(critical_failure_codes)
    if critical_failure_codes:
        if not dispatch_terminal_status:
            dispatch_terminal_status = "failed"
            dispatch_terminal_completed = False
        if not dispatch_terminal_failure_reason:
            dispatch_terminal_failure_reason = critical_failure_codes[0]
        if critical_failure_detail and not dispatch_terminal_failure_detail:
            dispatch_terminal_failure_detail = critical_failure_detail
    worker_unavailable_with_tool_expectation = (
        tool_plan_stage_event_count > 0
        or tool_execute_stage_event_count > 0
        or tool_call_start_event_count > 0
        or parse_error_invocation_count > 0
        or validation_error_invocation_count > 0
        or missing_tool_parse_error_count > 0
        or missing_tool_retry_exhausted_count > 0
        or missing_tool_unresolved_count > 0
    )
    if (
        tool_route_selected
        and observed_executed_count <= 0
        and worker_unavailable_event_count > 0
        and worker_unavailable_with_tool_expectation
    ):
        failure_codes.append("worker_unavailable_zero_execution")
    if missing_tool_parse_error_count > 0 or parse_error_invocation_count > 0:
        failure_codes.append("missing_tool_call_parse_error")
    if missing_tool_retry_exhausted_count > 0:
        failure_codes.append("missing_tool_call_retry_exhausted")
    if (
        observed_executed_count <= 0
        and missing_tool_unresolved_count > 0
        and tool_plan_stage_event_count > 0
        and tool_execute_stage_event_count <= 0
    ):
        failure_codes.append("missing_tool_call_unresolved")
    if tool_route_selected and observed_executed_count <= 0:
        if workflow_handoff_failure_reason:
            failure_codes.append(workflow_handoff_failure_reason)
        if contract_resolution_status == "missing":
            failure_codes.append("tool_dispatch_contract_unresolved")
        if dispatch_terminal_status == "missing":
            failure_codes.append("tool_dispatch_workflow_missing")
        if not dispatch_boundary_events:
            failure_codes.append("tool_dispatch_boundary_missing")
        elif (
            not workflow_handoff_started and selected_execution_mode == "tool_pipeline"
        ):
            failure_codes.append("tool_dispatch_not_started")
        elif workflow_handoff_started and observed_started_count <= 0:
            if dispatch_terminal_failure_reason:
                failure_codes.append(dispatch_terminal_failure_reason)
            failure_codes.append("tool_dispatch_handoff_zero_execution")
        elif dispatch_terminal_status == "failed":
            if dispatch_terminal_failure_reason:
                failure_codes.append(dispatch_terminal_failure_reason)
            failure_codes.append("tool_dispatch_failed_before_tool_execution")
    if (
        selected_execution_mode == "custom_workflow"
        and not dispatch_boundary_events
        and not workflow_instance_submission_events
        and not dispatch_terminal_status
        and not _custom_workflow_execution_progress_observed(custom_workflow_execution)
    ):
        failure_codes.append("custom_workflow_dispatch_not_started")

    deduped_failure_codes: list[str] = []
    seen_failure_codes: set[str] = set()
    for code in failure_codes:
        cleaned_code = _safe_str(code)
        if not cleaned_code:
            continue
        lowered = cleaned_code.lower()
        if lowered in seen_failure_codes:
            continue
        seen_failure_codes.add(lowered)
        deduped_failure_codes.append(cleaned_code)

    planned_count = max(
        observed_started_count,
        invocation_count,
        (
            1
            if (
                tool_route_selected
                and (deduped_failure_codes or dispatch_boundary_events)
            )
            else 0
        ),
    )
    tool_execution = {
        "planned_count": planned_count,
        "started_count": observed_started_count,
        "executed_count": observed_executed_count,
        "invocation_count": invocation_count,
        "successful_invocation_count": successful_invocation_count,
        "failed_invocation_count": failed_invocation_count,
        "blocked_invocation_count": blocked_invocation_count,
        "worker_unavailable_event_count": worker_unavailable_event_count,
        "tool_plan_stage_event_count": tool_plan_stage_event_count,
        "tool_execute_stage_event_count": tool_execute_stage_event_count,
        "parse_error_invocation_count": parse_error_invocation_count,
        "validation_error_invocation_count": validation_error_invocation_count,
        "failure_codes": list(deduped_failure_codes),
        "zero_tools_executed": observed_executed_count <= 0,
    }
    zero_tool_reason = _derive_zero_tool_execution_reason(
        selected_execution_mode=selected_execution_mode,
        tool_route_selected=tool_route_selected,
        tool_executed_count=observed_executed_count,
        failure_codes=deduped_failure_codes,
        dispatch_terminal_status=dispatch_terminal_status,
        dispatch_terminal_completed=dispatch_terminal_completed,
        custom_workflow_execution=custom_workflow_execution,
    )

    last_successful_boundary = ""
    if selected_workflow_id or selector_verdict:
        last_successful_boundary = "workflow_selected"
    if execution_mode_selected_explicitly:
        last_successful_boundary = "execution_mode_selected"
    if contract_resolution_status == "resolved":
        last_successful_boundary = "contract_resolution"
    if workflow_handoff_started:
        last_successful_boundary = "workflow_handoff"
    if observed_started_count > 0:
        last_successful_boundary = "tool_execution_started"
    if observed_executed_count > 0:
        last_successful_boundary = "tool_execution_completed"
    if dispatch_terminal_status:
        last_successful_boundary = "workflow_terminal"

    return {
        "tool_route_selected": tool_route_selected,
        "selected_workflow_id": selected_workflow_id,
        "selector_verdict": selector_verdict,
        "selected_execution_mode": selected_execution_mode or None,
        "dispatch_workflow_id": dispatch_workflow_id or None,
        "dispatch_event_count": len(dispatch_boundary_events),
        "contract_resolution_status": contract_resolution_status or None,
        "workflow_handoff_started": workflow_handoff_started,
        "workflow_handoff_failure_reason": workflow_handoff_failure_reason or None,
        "workflow_handoff_failure_error_class": (
            workflow_handoff_failure_error_class or None
        ),
        "dispatch_terminal_status": dispatch_terminal_status or None,
        "dispatch_terminal_final_state": dispatch_terminal_final_state or None,
        "dispatch_terminal_completed": dispatch_terminal_completed,
        "dispatch_terminal_failure_reason": dispatch_terminal_failure_reason or None,
        "dispatch_terminal_failure_error_class": (
            dispatch_terminal_failure_error_class or None
        ),
        "dispatch_terminal_failure_detail": dispatch_terminal_failure_detail or None,
        "dispatch_terminal_failing_state_id": (
            dispatch_terminal_failing_state_id or None
        ),
        "dispatch_terminal_failing_action_id": (
            dispatch_terminal_failing_action_id or None
        ),
        "dispatch_terminal_unresolved_required_inputs": (
            list(dispatch_terminal_unresolved_required_inputs)
            if dispatch_terminal_unresolved_required_inputs
            else []
        ),
        "dispatch_terminal_launch_input_resolution_status": (
            dispatch_terminal_launch_input_resolution_status or None
        ),
        "last_successful_boundary": last_successful_boundary or None,
        "planned_count": tool_execution["planned_count"],
        "started_count": tool_execution["started_count"],
        "executed_count": tool_execution["executed_count"],
        "invocation_count": tool_execution["invocation_count"],
        "successful_invocation_count": tool_execution["successful_invocation_count"],
        "failed_invocation_count": tool_execution["failed_invocation_count"],
        "blocked_invocation_count": tool_execution["blocked_invocation_count"],
        "worker_unavailable_event_count": tool_execution[
            "worker_unavailable_event_count"
        ],
        "tool_plan_stage_event_count": tool_execution["tool_plan_stage_event_count"],
        "tool_execute_stage_event_count": tool_execution[
            "tool_execute_stage_event_count"
        ],
        "failure_codes": list(tool_execution["failure_codes"]),
        "zero_tools_executed": tool_execution["zero_tools_executed"],
        "parse_error_invocation_count": tool_execution["parse_error_invocation_count"],
        "validation_error_invocation_count": tool_execution[
            "validation_error_invocation_count"
        ],
        "tool_execution": tool_execution,
        "custom_workflow_execution": custom_workflow_execution,
        "zero_tool_reason_code": zero_tool_reason["zero_tool_reason_code"],
        "zero_tool_reason": zero_tool_reason["zero_tool_reason"],
        "zero_tool_execution_expected": zero_tool_reason[
            "zero_tool_execution_expected"
        ],
    }


def _infer_tool_execution_required_effect(
    *,
    execution_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(execution_summary, Mapping):
        return None
    if not bool(execution_summary.get("tool_route_selected")):
        return None

    executed_count = _safe_non_negative_int(execution_summary.get("executed_count"))
    if executed_count > 0:
        return None

    failure_codes = _normalise_failure_codes(execution_summary.get("failure_codes"))
    if not failure_codes:
        return None

    primary_failure_code = failure_codes[0]
    status_reason = _TOOL_EXECUTION_FAILURE_REASON_MAP.get(
        primary_failure_code,
        "Tool-calling workflow selected but no tool execution was observed.",
    )

    return {
        "effect_id": "effect_tool_execution_1",
        "intent_origin": "workflow_contract",
        "effect_type": "tool_execution",
        "description": "Run at least one tool call for this tool-calling turn.",
        "required_tools": [],
        "targets": [],
        "required_predicates": [],
        "postcondition_required": True,
        "postcondition_strategy": "execution_observed",
        "status": "not_executed",
        "status_reason": status_reason,
        "failure_code": primary_failure_code,
        "failure_codes": list(failure_codes),
    }


def _infer_custom_workflow_required_effect(
    *,
    execution_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(execution_summary, Mapping):
        return None
    if (_safe_str(execution_summary.get("selected_execution_mode")) or "").lower() != (
        "custom_workflow"
    ):
        return None

    dispatch_terminal_status = (
        _safe_str(execution_summary.get("dispatch_terminal_status")) or ""
    ).lower()
    if dispatch_terminal_status != "failed":
        return None

    custom_workflow_execution = execution_summary.get("custom_workflow_execution")
    custom_workflow_summary = (
        custom_workflow_execution
        if isinstance(custom_workflow_execution, Mapping)
        else {}
    )

    execution_progress_observed = _custom_workflow_execution_progress_observed(
        custom_workflow_summary
    )

    failure_codes: list[str] = []
    primary_failure_code = _safe_str(
        execution_summary.get("dispatch_terminal_failure_reason")
    )
    if primary_failure_code:
        failure_codes.append(primary_failure_code)
    zero_tool_reason_code = _safe_str(execution_summary.get("zero_tool_reason_code"))
    if zero_tool_reason_code and zero_tool_reason_code not in failure_codes:
        failure_codes.append(zero_tool_reason_code)
    if not failure_codes:
        failure_codes.append("custom_workflow_dispatch_failed")

    status_reason = _safe_str(execution_summary.get("dispatch_terminal_failure_detail"))
    if not status_reason:
        dispatch_workflow_id = _safe_str(execution_summary.get("dispatch_workflow_id"))
        failing_state_id = _safe_str(
            execution_summary.get("dispatch_terminal_failing_state_id")
        )
        failing_action_id = _safe_str(
            execution_summary.get("dispatch_terminal_failing_action_id")
        )
        if execution_progress_observed:
            status_reason = _CUSTOM_WORKFLOW_EXECUTION_FAILURE_REASON
        else:
            workflow_label = dispatch_workflow_id or "Selected workflow"
            status_reason = (
                f"{workflow_label} failed before any workflow action could start."
            )
        if failing_state_id:
            status_reason += f" Failing state: {failing_state_id}."
        if failing_action_id:
            status_reason += f" Failing action: {failing_action_id}."

    return {
        "effect_id": "effect_workflow_execution_1",
        "intent_origin": "workflow_contract",
        "effect_type": "workflow_execution",
        "description": "Obtain the selected workflow result needed for the user-facing answer.",
        "required_tools": [],
        "targets": [],
        "required_predicates": [],
        "postcondition_required": True,
        "postcondition_strategy": "workflow_terminal_success",
        "status": "not_satisfied" if execution_progress_observed else "not_executed",
        "status_reason": status_reason,
        "failure_code": failure_codes[0],
        "failure_codes": list(failure_codes),
        "workflow_id": _safe_str(execution_summary.get("dispatch_workflow_id")),
    }


def _dedupe_string_sequence(values: Sequence[Any]) -> list[str]:
    cleaned_values: list[str] = []
    seen: set[str] = set()
    for raw in values:
        cleaned = _safe_str(raw)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned_values.append(cleaned)
    return cleaned_values


def _tool_requirement_key(tool_name: Any) -> str:
    return canonical_required_tool_key(tool_name)


def _extract_string_sequence_from_mapping(
    payload: Mapping[str, Any] | None,
    key: str,
) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return _dedupe_string_sequence(value)


def _extract_required_tool_obligation_ledger(
    *sources: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        if isinstance(source.get("obligations"), list):
            return dict(source)
        nested = source.get("required_tool_obligation_ledger")
        if isinstance(nested, Mapping):
            return dict(nested)
        envelope = source.get("llm_step_envelope")
        if isinstance(envelope, Mapping) and isinstance(
            envelope.get("required_tool_obligation_ledger"), Mapping
        ):
            return dict(envelope["required_tool_obligation_ledger"])
    return None


def _extract_invocation_concept_argument(invocation: Mapping[str, Any]) -> str | None:
    for field_name in (
        "effective_arguments",
        "arguments",
        "effective_payload",
        "payload",
    ):
        value = invocation.get(field_name)
        if not isinstance(value, Mapping):
            continue
        concept_id = _safe_str(value.get("concept_id"))
        if concept_id:
            return concept_id
    return None


def _extract_resolved_concept_ids_from_invocations(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    concept_ids: list[str] = []
    seen: set[str] = set()

    def _append(raw_value: Any) -> None:
        concept_id = _safe_str(raw_value)
        if not concept_id:
            return
        lowered = concept_id.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        concept_ids.append(concept_id)

    for invocation in tool_invocations or ():
        if not isinstance(invocation, Mapping):
            continue
        tool_name = _safe_str(invocation.get("tool")) or _safe_str(
            invocation.get("method")
        )
        if not tool_name or tool_name.lower() != "resolve_concept_by_name":
            continue
        payload = invocation.get("effective_payload")
        if not isinstance(payload, Mapping):
            payload = invocation.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if (
            _classify_tool_invocation_status(invocation=invocation, payload=payload)
            != "ok"
        ):
            continue

        _append(payload.get("resolved_concept_id"))
        _append(payload.get("concept_id"))
        match = payload.get("match")
        if isinstance(match, Mapping):
            _append(match.get("concept_id"))
            _append(match.get("resolved_concept_id"))

    return concept_ids


def _search_concepts_row_is_follow_up_concept(row: Mapping[str, Any]) -> bool:
    concept_id = _safe_str(row.get("concept_id"))
    if not concept_id:
        return False
    lowered_concept_id = concept_id.lower()
    if lowered_concept_id.startswith("#v#uploaded_file_copy_"):
        return False

    predicate_meta_ids = {
        "#v#predicate",
        "#v#binary_predicate",
        "#v#ternary_predicate",
        "#v#nary_predicate",
    }
    if lowered_concept_id in predicate_meta_ids:
        return False

    kind = _safe_str(row.get("kind"))
    if kind and kind.lower() == "predicate":
        return False

    instance_of = _safe_str(row.get("instance_of"))
    if instance_of and instance_of.lower() in predicate_meta_ids:
        return False

    hierarchy = row.get("hierarchy")
    if isinstance(hierarchy, Mapping):
        primary_path = hierarchy.get("primary_path")
        if isinstance(primary_path, Sequence) and not isinstance(
            primary_path,
            (str, bytes, bytearray),
        ):
            lowered_path = {
                str(item).strip().lower()
                for item in primary_path
                if isinstance(item, str) and str(item).strip()
            }
            if lowered_path & predicate_meta_ids:
                return False

    return True


def _extract_search_concept_ids_from_invocations(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    concept_ids: list[str] = []
    seen: set[str] = set()

    for invocation in tool_invocations or ():
        if not isinstance(invocation, Mapping):
            continue
        tool_name = _safe_str(invocation.get("tool")) or _safe_str(
            invocation.get("method")
        )
        if not tool_name or tool_name.lower() not in {
            "search_concepts",
            "vontology_concept_search",
        }:
            continue
        payload = invocation.get("effective_payload")
        if not isinstance(payload, Mapping):
            payload = invocation.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if (
            _classify_tool_invocation_status(invocation=invocation, payload=payload)
            != "ok"
        ):
            continue

        results = payload.get("results")
        if not isinstance(results, Sequence) or isinstance(
            results,
            (str, bytes, bytearray),
        ):
            continue
        for row in results:
            if not isinstance(row, Mapping):
                continue
            if not _search_concepts_row_is_follow_up_concept(row):
                continue
            concept_id = _safe_str(row.get("concept_id"))
            if not concept_id:
                continue
            lowered = concept_id.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            concept_ids.append(concept_id)

    return concept_ids


def _extract_fetch_concept_ids_from_invocations(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    concept_ids: list[str] = []
    seen: set[str] = set()

    for invocation in tool_invocations or ():
        if not isinstance(invocation, Mapping):
            continue
        tool_name = _safe_str(invocation.get("tool")) or _safe_str(
            invocation.get("method")
        )
        if not tool_name or tool_name.lower() != "fetch_concept":
            continue
        payload = invocation.get("effective_payload")
        if not isinstance(payload, Mapping):
            payload = invocation.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        if (
            _classify_tool_invocation_status(invocation=invocation, payload=payload)
            != "ok"
        ):
            continue
        concept_id = _extract_invocation_concept_argument(invocation)
        if not concept_id:
            continue
        lowered = concept_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        concept_ids.append(concept_id)

    return concept_ids


def _target_bound_missing_prompt_tools(
    *,
    required_tools: Sequence[str],
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    required_lookup = {
        tool_name.lower()
        for tool_name in required_tools
        if isinstance(tool_name, str) and tool_name.strip()
    }
    if "get_predicate_incidence" not in required_lookup:
        return []
    if not (
        {"resolve_concept_by_name", "search_concepts", "vontology_concept_search"}
        & required_lookup
    ):
        return []

    target_concept_ids = _dedupe_string_sequence(
        [
            *_extract_resolved_concept_ids_from_invocations(tool_invocations),
            *_extract_search_concept_ids_from_invocations(tool_invocations),
            *_extract_fetch_concept_ids_from_invocations(tool_invocations),
        ]
    )
    target_lookup = {concept_id.lower() for concept_id in target_concept_ids}
    targeted_incidence_observed = False
    if target_lookup:
        for invocation in tool_invocations or ():
            if not isinstance(invocation, Mapping):
                continue
            tool_name = _safe_str(invocation.get("tool")) or _safe_str(
                invocation.get("method")
            )
            if not tool_name or tool_name.lower() != "get_predicate_incidence":
                continue
            payload = invocation.get("effective_payload")
            if not isinstance(payload, Mapping):
                payload = invocation.get("payload")
            if not isinstance(payload, Mapping):
                payload = {}
            if (
                _classify_tool_invocation_status(invocation=invocation, payload=payload)
                != "ok"
            ):
                continue
            concept_id = _extract_invocation_concept_argument(invocation)
            if concept_id and concept_id.lower() in target_lookup:
                targeted_incidence_observed = True
                break

    return [] if targeted_incidence_observed else ["get_predicate_incidence"]


def _extract_sequence_values_from_aux(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    *,
    field_names: Sequence[str],
    entry_types: Sequence[str],
) -> list[str]:
    values: list[str] = []
    allowed_entry_types = {entry_type.lower() for entry_type in entry_types}
    for entry in aux_llm_calls or ():
        if not isinstance(entry, Mapping):
            continue
        entry_type = (_safe_str(entry.get("type")) or "").strip().lower()
        if entry_type not in allowed_entry_types:
            continue
        for field_name in field_names:
            raw_values = entry.get(field_name)
            if not isinstance(raw_values, Sequence) or isinstance(
                raw_values, (str, bytes)
            ):
                continue
            values.extend(raw_values)
    return _dedupe_string_sequence(values)


def _extract_required_scholarly_representation_file_copy_ids_from_aux(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    return _extract_sequence_values_from_aux(
        aux_llm_calls,
        field_names=_AUX_REQUIRED_SCHOLARLY_REPRESENTATION_FILE_COPY_ID_FIELDS,
        entry_types=("prompt_tool_requirements", "workflow_selector_override"),
    )


def _extract_required_representation_file_copy_ids_from_aux(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    return _extract_sequence_values_from_aux(
        aux_llm_calls,
        field_names=_AUX_REQUIRED_REPRESENTATION_FILE_COPY_ID_FIELDS,
        entry_types=("prompt_tool_requirements", "workflow_selector_override"),
    )


def _extract_required_tool_names_from_aux(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    return _extract_sequence_values_from_aux(
        aux_llm_calls,
        field_names=_AUX_REQUIRED_TOOL_FIELDS,
        entry_types=(
            "prompt_tool_requirements",
            "prompt_tool_requirements_preflight",
        ),
    )


def _extract_tool_call_validation_failure_context(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    failures_by_tool: dict[str, dict[str, Any]] = {}
    ordered_failed_tools: list[str] = []
    repair_outcome = ""
    repair_stop_reason = ""
    repair_decision: dict[str, Any] | None = None

    for entry in aux_llm_calls or ():
        if not isinstance(entry, Mapping):
            continue
        entry_type = (_safe_str(entry.get("type")) or "").lower()
        if entry_type == "tool_call_validation_repair_result":
            raw_decision = entry.get("decision")
            repair_decision = (
                dict(raw_decision) if isinstance(raw_decision, Mapping) else None
            )
            if isinstance(repair_decision, Mapping):
                repair_outcome = (
                    "repair_succeeded"
                    if bool(repair_decision.get("succeeded"))
                    else "repair_failed"
                )
                repair_stop_reason = _safe_str(repair_decision.get("reason"))
            continue
        if entry_type != "tool_contract_attempt":
            continue

        diagnostics = entry.get("diagnostics")
        if not isinstance(diagnostics, list):
            diagnostics = []
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, Mapping):
                continue
            tool_name = _safe_str(diagnostic.get("tool"))
            if not tool_name:
                continue
            tool_key = _tool_requirement_key(tool_name)
            if tool_key not in failures_by_tool:
                failures_by_tool[tool_key] = {
                    "tool": tool_name,
                    "errors": [],
                }
                ordered_failed_tools.append(tool_name)
            error_payload: dict[str, Any] = {
                "tool": tool_name,
                "error_code": _safe_str(diagnostic.get("error_code")),
                "message": _safe_str(diagnostic.get("message")),
            }
            payload = diagnostic.get("payload")
            if isinstance(payload, Mapping):
                error_payload["payload"] = dict(payload)
            contract = diagnostic.get("contract")
            if isinstance(contract, Mapping):
                error_payload["contract"] = dict(contract)
            failures_by_tool[tool_key]["errors"].append(
                {key: value for key, value in error_payload.items() if value}
            )

        if diagnostics:
            continue
        tool_calls = entry.get("tool_calls")
        validation_errors: list[str] = []
        for raw_error in entry.get("validation_errors") or []:
            error = _safe_str(raw_error)
            if error:
                validation_errors.append(error)
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, Mapping):
                continue
            tool_name = _safe_str(tool_call.get("tool"))
            if not tool_name:
                continue
            matching_errors = [
                error
                for error in validation_errors
                if error.lower().startswith(f"{tool_name.lower()}:")
            ]
            if not matching_errors:
                continue
            tool_key = _tool_requirement_key(tool_name)
            if tool_key not in failures_by_tool:
                failures_by_tool[tool_key] = {
                    "tool": tool_name,
                    "errors": [],
                }
                ordered_failed_tools.append(tool_name)
            for error in matching_errors:
                failures_by_tool[tool_key]["errors"].append(
                    {
                        "tool": tool_name,
                        "error_code": "schema_validation_failed",
                        "message": error,
                    }
                )

    return {
        "failed_tools": ordered_failed_tools,
        "failures_by_tool": failures_by_tool,
        "repair_outcome": repair_outcome,
        "repair_stop_reason": repair_stop_reason,
        "repair_decision": repair_decision,
    }


def _apply_tool_call_validation_failures_to_required_effects(
    *,
    required_effects: Sequence[Mapping[str, Any]],
    validation_failure_context: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(validation_failure_context, Mapping):
        return [
            dict(effect) for effect in required_effects if isinstance(effect, Mapping)
        ]
    failures_by_tool_raw = validation_failure_context.get("failures_by_tool")
    if not isinstance(failures_by_tool_raw, Mapping) or not failures_by_tool_raw:
        return [
            dict(effect) for effect in required_effects if isinstance(effect, Mapping)
        ]

    failures_by_tool: dict[str, Mapping[str, Any]] = {
        _tool_requirement_key(tool_key): failure
        for tool_key, failure in failures_by_tool_raw.items()
        if isinstance(failure, Mapping) and _tool_requirement_key(tool_key)
    }
    repair_outcome = _safe_str(validation_failure_context.get("repair_outcome"))
    repair_stop_reason = _safe_str(validation_failure_context.get("repair_stop_reason"))
    repair_decision = validation_failure_context.get("repair_decision")
    updated_effects: list[dict[str, Any]] = []

    for raw_effect in required_effects:
        if not isinstance(raw_effect, Mapping):
            continue
        effect = dict(raw_effect)
        required_tools = _dedupe_string_sequence(effect.get("required_tools") or [])
        matched_failure: Mapping[str, Any] | None = None
        for tool_name in required_tools:
            failure = failures_by_tool.get(_tool_requirement_key(tool_name))
            if isinstance(failure, Mapping):
                matched_failure = failure
                break
        if matched_failure is None:
            updated_effects.append(effect)
            continue

        errors = [
            dict(error)
            for error in (matched_failure.get("errors") or [])
            if isinstance(error, Mapping)
        ]
        first_message = next(
            (
                _safe_str(error.get("message"))
                for error in errors
                if _safe_str(error.get("message"))
            ),
            "",
        )
        first_error_code = next(
            (
                _safe_str(error.get("error_code"))
                for error in errors
                if _safe_str(error.get("error_code"))
            ),
            "",
        )
        failure_code = (
            _safe_str(effect.get("failed_failure_code"))
            or f"{_effect_failure_code_slug(effect)}_validation_failed"
        )
        failure_codes = _normalise_failure_codes(effect.get("failure_codes"))
        for code in (failure_code, first_error_code, "tool_call_validation_failed"):
            if code and code not in failure_codes:
                failure_codes.append(code)

        effect["status"] = "not_satisfied"
        effect["status_reason"] = (
            first_message
            or _safe_str(effect.get("failed_reason"))
            or "Required tool call failed validation before execution."
        )
        effect["failure_code"] = failure_code
        effect["failure_codes"] = failure_codes
        effect["tool_call_validation_errors"] = errors
        if repair_outcome:
            effect["tool_call_repair_outcome"] = repair_outcome
        if repair_stop_reason:
            effect["tool_call_repair_stop_reason"] = repair_stop_reason
        if isinstance(repair_decision, Mapping):
            effect["tool_call_repair_decision"] = dict(repair_decision)
        updated_effects.append(effect)

    return updated_effects


def _extract_required_scholarly_file_copy_ids_from_aux(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    # Backward-compatible helper retained for call sites that still expect the
    # scholarly-specific name while the contract path is being rolled out.
    return _extract_required_scholarly_representation_file_copy_ids_from_aux(
        aux_llm_calls
    )


def _normalise_representation_decision_policy(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, Mapping):
        return {}
    policy: dict[str, bool] = {}
    for raw_key, raw_value in raw.items():
        key = _safe_str(raw_key)
        if not key:
            continue
        policy[key] = bool(raw_value)
    return policy


def _load_representation_domain_profiles_from_vontology() -> (
    tuple[list[dict[str, Any]], dict[str, Any]]
):
    requested_profile_concept_ids = list(canonical_representation_profile_concept_ids())
    loaded_profiles, diagnostics = (
        load_representation_contract_profiles_from_concept_ids(
            requested_profile_concept_ids
        )
    )
    bootstrap_report: dict[str, Any] | None = None
    diagnostics_mapping = diagnostics if isinstance(diagnostics, Mapping) else {}
    missing_profile_concept_ids = _dedupe_string_sequence(
        diagnostics_mapping.get("missing_profile_concept_ids") or []
    )
    malformed_profile_concept_ids = _dedupe_string_sequence(
        diagnostics_mapping.get("malformed_profile_concept_ids") or []
    )
    if (
        not loaded_profiles
        or missing_profile_concept_ids
        or malformed_profile_concept_ids
    ):
        try:
            bootstrap_targets = (
                missing_profile_concept_ids
                or malformed_profile_concept_ids
                or requested_profile_concept_ids
            )
            bootstrap_report = ensure_canonical_representation_contract_profiles(
                concept_ids=bootstrap_targets,
                provenance={
                    "source": "turn_execution_record_service",
                    "reason": "representation_profile_catalogue_drift_repair",
                },
                context={"path": "_load_representation_domain_profiles_from_vontology"},
            )
        except Exception as exc:
            bootstrap_report = {
                "success": False,
                "reason": "bootstrap_exception",
                "error": str(exc),
            }

        loaded_profiles, diagnostics = (
            load_representation_contract_profiles_from_concept_ids(
                requested_profile_concept_ids
            )
        )

    normalised_profiles: list[dict[str, Any]] = []
    for profile in loaded_profiles:
        if not isinstance(profile, Mapping):
            continue
        profile_payload = dict(profile)
        profile_payload["profile_id"] = (
            _safe_str(profile_payload.get("profile_id")) or ""
        )
        profile_payload["profile_concept_id"] = _safe_str(
            profile_payload.get("profile_concept_id")
        ) or _safe_str(profile_payload.get("_source_concept_id"))
        profile_payload["target_entity_class"] = (
            _safe_str(profile_payload.get("target_entity_class")) or "thing"
        )
        profile_payload["effect_type"] = (
            _safe_str(profile_payload.get("effect_type"))
            or "representation_profile_guard"
        )
        profile_payload["description"] = (
            _safe_str(profile_payload.get("description"))
            or "Ensure the requested representation is materialised from the artefact context."
        )
        profile_payload["required_predicates"] = _dedupe_string_sequence(
            profile_payload.get("required_predicates") or []
        )

        normalised_tools_by_source: dict[str, list[str]] = {}
        raw_tools_by_source = profile_payload.get("required_tools_by_source")
        if isinstance(raw_tools_by_source, Mapping):
            for source, raw_tools in raw_tools_by_source.items():
                source_key = (_safe_str(source) or "").lower()
                if not source_key:
                    continue
                if isinstance(raw_tools, Sequence) and not isinstance(
                    raw_tools, (str, bytes)
                ):
                    normalised_tools_by_source[source_key] = _dedupe_string_sequence(
                        raw_tools
                    )
        profile_payload["required_tools_by_source"] = normalised_tools_by_source
        profile_payload["default_decision_policy"] = (
            _normalise_representation_decision_policy(
                profile_payload.get("default_decision_policy")
            )
        )

        if not profile_payload["profile_id"]:
            continue
        normalised_profiles.append(profile_payload)

    diagnostics_payload = dict(diagnostics) if isinstance(diagnostics, Mapping) else {}
    diagnostics_payload.setdefault(
        "representation_profile_source",
        "vontology_concept_text_relations",
    )
    diagnostics_payload.setdefault(
        "requested_concept_ids",
        list(requested_profile_concept_ids),
    )
    diagnostics_payload["loaded_profile_count"] = len(normalised_profiles)
    if not _safe_str(diagnostics_payload.get("profile_version_hash")):
        diagnostics_payload["profile_version_hash"] = _hash_payload(normalised_profiles)
    if isinstance(bootstrap_report, Mapping):
        diagnostics_payload["bootstrap"] = dict(bootstrap_report)
    return normalised_profiles, diagnostics_payload


def _find_representation_profile(
    *,
    profiles: Sequence[Mapping[str, Any]],
    profile_id: str,
) -> dict[str, Any] | None:
    for profile in profiles:
        if not isinstance(profile, Mapping):
            continue
        if (_safe_str(profile.get("profile_id")) or "").lower() == profile_id.lower():
            return dict(profile)
    return None


def _resolve_representation_required_tools(
    *,
    profile: Mapping[str, Any],
    artefact_source: str,
) -> list[str]:
    by_source = profile.get("required_tools_by_source")
    if not isinstance(by_source, Mapping):
        return []
    tools = by_source.get(artefact_source)
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        tools = by_source.get("unknown")
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        return []
    return _dedupe_string_sequence(tools)


def _build_representation_required_effect_template(
    *,
    profile: Mapping[str, Any],
    required_tools: Sequence[str],
    targets: Sequence[str],
) -> dict[str, Any]:
    profile_id = _safe_str(profile.get("profile_id")) or "representation"
    profile_concept_id = _safe_str(profile.get("profile_concept_id"))
    effect_type = (
        _safe_str(profile.get("effect_type")) or f"representation_{profile_id}"
    )
    description = (
        _safe_str(profile.get("description"))
        or "Ensure the requested representation is materialised from the artefact context."
    )
    required_predicates = profile.get("required_predicates")
    if not isinstance(required_predicates, Sequence) or isinstance(
        required_predicates, (str, bytes)
    ):
        required_predicates = []

    return {
        "effect_id": f"effect_{profile_id}_representation_1",
        "intent_origin": "workflow_contract",
        "effect_type": effect_type,
        "representation_domain_id": profile_id,
        "representation_profile_concept_id": profile_concept_id,
        "description": description,
        "required_tools": _dedupe_string_sequence(required_tools),
        "targets": _dedupe_string_sequence(targets),
        "required_predicates": _dedupe_string_sequence(required_predicates),
        "postcondition_required": True,
        "postcondition_strategy": "execution_observed",
    }


def _classify_representation_target_source(target: str) -> str:
    lowered = target.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "url"
    if extract_arxiv_id_candidates(target):
        return "url"
    if lowered.startswith("#v#") and "file_copy" in lowered:
        return "file_copy"
    return "unknown"


def _build_representation_effects_for_targets(
    *,
    profile: Mapping[str, Any],
    targets: Sequence[str],
    write_request_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    profile_id = _safe_str(profile.get("profile_id")) or "representation"
    for index, target in enumerate(_dedupe_string_sequence(targets), start=1):
        target_source = _classify_representation_target_source(target)
        required_tools = _resolve_representation_required_tools(
            profile=profile,
            artefact_source=target_source,
        )
        if not required_tools:
            continue
        filtered_required_tools = [
            tool_name
            for tool_name in required_tools
            if not _write_request_evidence_denies_tool(
                write_request_evidence,
                tool_name,
            )
        ]
        if not filtered_required_tools:
            continue
        effect = _build_representation_required_effect_template(
            profile=profile,
            required_tools=filtered_required_tools,
            targets=[target],
        )
        effect["effect_id"] = f"effect_{profile_id}_representation_{index}"
        effect["artefact_source"] = target_source
        effect["required_tools_match"] = (
            "all" if len(filtered_required_tools) > 1 else "any"
        )
        effect["status"] = "not_executed"
        effect["status_reason"] = (
            "No required representation tool execution was observed."
        )
        effect["failure_code"] = f"{profile_id}_representation_not_executed"
        effect["failure_codes"] = [f"{profile_id}_representation_not_executed"]
        effect["prompt_target"] = target
        effects.append(effect)
    return effects


def _build_fail_closed_representation_effect(
    *,
    failure_code: str,
    status_reason: str,
    targets: Sequence[str],
) -> dict[str, Any]:
    return {
        "effect_id": "effect_representation_contract_guard_1",
        "intent_origin": "workflow_contract",
        "effect_type": "representation_contract_guard",
        "representation_domain_id": "representation",
        "description": "Required representation contract profile resolution failed.",
        "required_tools": [],
        "targets": _dedupe_string_sequence(targets),
        "required_predicates": [],
        "postcondition_required": True,
        "postcondition_strategy": "execution_observed",
        "status": "not_executed",
        "status_reason": status_reason,
        "failure_code": failure_code,
        "failure_codes": [failure_code],
    }


def _build_fail_closed_representation_contract(
    *,
    contract_id_suffix: str,
    targets: Sequence[str],
    artefact_context: Mapping[str, Any],
    diagnostics: Mapping[str, Any] | None,
    failure_code: str,
    status_reason: str,
    requested_profile_id: str | None = None,
) -> dict[str, Any]:
    requested_profile_concept_ids = (
        _dedupe_string_sequence(diagnostics.get("requested_concept_ids") or [])
        if isinstance(diagnostics, Mapping)
        else []
    )
    loaded_profile_concept_ids = (
        _dedupe_string_sequence(diagnostics.get("loaded_concept_ids") or [])
        if isinstance(diagnostics, Mapping)
        else []
    )
    return {
        "schema_version": _REPRESENTATION_CONTRACT_SCHEMA_VERSION,
        "contract_id": (
            "structured_representation_"
            f"{contract_id_suffix}_{_hash_payload(targets) or 'targets'}"
        ),
        "intent_class": "representation",
        "domain_profile_id": requested_profile_id or "representation",
        "artefact_context": dict(artefact_context),
        "profile_source": (
            _safe_str(diagnostics.get("representation_profile_source"))
            if isinstance(diagnostics, Mapping)
            else ""
        ),
        "profile_version_hash": (
            _safe_str(diagnostics.get("profile_version_hash"))
            if isinstance(diagnostics, Mapping)
            else ""
        ),
        "profile_resolution": {
            "requested_profile_concept_ids": requested_profile_concept_ids,
            "loaded_profile_concept_ids": loaded_profile_concept_ids,
            "selected_profile_concept_id": "",
            "selected_profile_id": requested_profile_id or "",
            "fail_closed": True,
            "fail_closed_reason": failure_code,
        },
        "required_effects": [
            _build_fail_closed_representation_effect(
                failure_code=failure_code,
                status_reason=status_reason,
                targets=targets,
            )
        ],
    }


def _extract_applied_workflow_continuation_context(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    if not isinstance(aux_llm_calls, Sequence) or isinstance(
        aux_llm_calls, (str, bytes)
    ):
        return None
    for entry in reversed(aux_llm_calls):
        if not isinstance(entry, Mapping):
            continue
        if _safe_str(entry.get("type")) != "workflow_continuation_context":
            continue
        if not bool(entry.get("applied", False)):
            continue
        context = entry.get("context")
        if isinstance(context, Mapping):
            return dict(context)
    return None


def _required_effects_contract_from_continuation_context(
    continuation_context: Mapping[str, Any] | None,
    *,
    intent_class: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(continuation_context, Mapping):
        return None
    contract = continuation_context.get("required_effects_contract")
    if not isinstance(contract, Mapping):
        return None
    contract_intent_class = _safe_str(contract.get("intent_class"))
    schema_version = _safe_str(contract.get("schema_version"))
    if intent_class and contract_intent_class != intent_class:
        return None
    if schema_version and schema_version != _REPRESENTATION_CONTRACT_SCHEMA_VERSION:
        return None
    return dict(contract)


def _representation_contract_from_continuation_context(
    continuation_context: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    return _required_effects_contract_from_continuation_context(
        continuation_context,
        intent_class="representation",
    )


def _build_representation_required_effects_contract(
    *,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    continuation_context = _extract_applied_workflow_continuation_context(aux_llm_calls)
    continuation_contract = _representation_contract_from_continuation_context(
        continuation_context
    )
    if isinstance(continuation_contract, Mapping):
        return continuation_contract

    scholarly_file_copy_ids = (
        _extract_required_scholarly_representation_file_copy_ids_from_aux(aux_llm_calls)
    )
    generic_file_copy_ids = _extract_required_representation_file_copy_ids_from_aux(
        aux_llm_calls
    )
    if not scholarly_file_copy_ids and not generic_file_copy_ids:
        return None

    profiles, diagnostics = _load_representation_domain_profiles_from_vontology()
    if scholarly_file_copy_ids:
        profile = _find_representation_profile(profiles=profiles, profile_id="paper")
        if profile is None:
            failure_code = (
                _REPRESENTATION_CONFIG_UNAVAILABLE_FAILURE_CODE
                if not profiles
                else _REPRESENTATION_PROFILE_UNMATCHED_FAILURE_CODE
            )
            return _build_fail_closed_representation_contract(
                contract_id_suffix="paper",
                targets=scholarly_file_copy_ids,
                artefact_context={"file_copy_ids": list(scholarly_file_copy_ids)},
                diagnostics=diagnostics,
                failure_code=failure_code,
                status_reason=(
                    "Structured scholarly representation targets were recorded, but "
                    "the authoritative paper representation profile could not be resolved."
                ),
                requested_profile_id="paper",
            )

        required_effects = _build_representation_effects_for_targets(
            profile=profile,
            targets=scholarly_file_copy_ids,
            write_request_evidence=_extract_write_request_evidence_from_aux(
                aux_llm_calls
            ),
        )
        if not required_effects:
            failure_code = "paper_representation_requirements_missing"
            return _build_fail_closed_representation_contract(
                contract_id_suffix="paper",
                targets=scholarly_file_copy_ids,
                artefact_context={"file_copy_ids": list(scholarly_file_copy_ids)},
                diagnostics=diagnostics,
                failure_code=failure_code,
                status_reason=(
                    "The authoritative paper representation profile did not resolve "
                    "any required tools for the structured scholarly targets."
                ),
                requested_profile_id="paper",
            )

        default_decision_policy = profile.get("default_decision_policy")
        profile_resolution = {
            "requested_profile_concept_ids": (
                _dedupe_string_sequence(diagnostics.get("requested_concept_ids") or [])
                if isinstance(diagnostics, Mapping)
                else []
            ),
            "loaded_profile_concept_ids": (
                _dedupe_string_sequence(diagnostics.get("loaded_concept_ids") or [])
                if isinstance(diagnostics, Mapping)
                else []
            ),
            "selected_profile_concept_id": _safe_str(profile.get("profile_concept_id")),
            "selected_profile_id": "paper",
            "fail_closed": False,
            "fail_closed_reason": "",
        }
        contract_payload: dict[str, Any] = {
            "schema_version": _REPRESENTATION_CONTRACT_SCHEMA_VERSION,
            "contract_id": (
                "structured_representation_paper_"
                f"{_hash_payload(scholarly_file_copy_ids) or 'targets'}"
            ),
            "intent_class": "representation",
            "domain_profile_id": "paper",
            "domain_profile_concept_id": _safe_str(profile.get("profile_concept_id")),
            "artefact_context": {"file_copy_ids": list(scholarly_file_copy_ids)},
            "profile_source": (
                _safe_str(diagnostics.get("representation_profile_source"))
                if isinstance(diagnostics, Mapping)
                else ""
            ),
            "profile_version_hash": (
                _safe_str(diagnostics.get("profile_version_hash"))
                if isinstance(diagnostics, Mapping)
                else ""
            ),
            "profile_resolution": profile_resolution,
            "required_effects": required_effects,
        }
        if isinstance(default_decision_policy, Mapping):
            contract_payload["default_decision_policy"] = dict(default_decision_policy)
        return contract_payload

    generic_targets = [
        target
        for target in generic_file_copy_ids
        if target.lower() not in {item.lower() for item in scholarly_file_copy_ids}
    ]
    if not generic_targets:
        return None

    return _build_fail_closed_representation_contract(
        contract_id_suffix="unbound",
        targets=generic_targets,
        artefact_context={"file_copy_ids": list(generic_targets)},
        diagnostics=diagnostics,
        failure_code=_REPRESENTATION_PROFILE_UNMATCHED_FAILURE_CODE,
        status_reason=(
            "Structured representation targets were recorded without an "
            "authoritative representation contract binding."
        ),
    )


def _is_prompt_required_evidence_tool(tool_name: Any) -> bool:
    cleaned = _safe_str(tool_name)
    if not cleaned:
        return False
    return is_tool_prompt_required_evidence(cleaned)


def _is_prompt_required_mutation_tool(tool_name: Any) -> bool:
    cleaned = _safe_str(tool_name)
    if not cleaned:
        return False
    return is_tool_prompt_required_mutation(cleaned)


def _tool_supports_concept_target(tool_name: Any) -> bool:
    lowered = (_safe_str(tool_name) or "").lower()
    return lowered in {
        "fetch_concept",
        "fetch_concept_content",
        "find_relations_with_argument",
        "get_predicate_incidence",
        "get_text_relations_summary",
    }


def _target_concept_ids_from_contract_surfaces(
    *surfaces: Mapping[str, Any] | None,
) -> list[str]:
    target_ids: list[str] = []
    for surface in surfaces:
        if not isinstance(surface, Mapping):
            continue
        for key in (
            "target_concept_ids",
            "required_prompt_fetch_concept_ids",
            "missing_prompt_fetch_concept_ids",
        ):
            target_ids.extend(_extract_string_sequence_from_mapping(surface, key))
        for key in (
            "turn_expected_outcome_contract_state",
            "expected_outcome_contract_state",
            "turn_expected_outcome_profile",
        ):
            nested = surface.get(key)
            if not isinstance(nested, Mapping):
                continue
            target_ids.extend(
                _extract_string_sequence_from_mapping(nested, "target_concept_ids")
            )
    return _dedupe_string_sequence(target_ids)


def _derive_required_evidence_target_concept_ids(
    *,
    prompt_text: Any,
    selected_workflow_id: str | None,
    resolved_turn_expected_outcome_contract: TurnExpectedOutcomeContract,
    selected_workflow_trace: Mapping[str, Any] | None,
    completion_report: Mapping[str, Any] | None,
) -> list[str]:
    target_ids = [
        *resolved_turn_expected_outcome_contract.target_concept_ids,
        *resolved_turn_expected_outcome_contract.target_type_ids,
        *_target_concept_ids_from_contract_surfaces(
            selected_workflow_trace,
            completion_report,
        ),
    ]
    if (
        selected_workflow_id
        and selected_workflow_id in _CONCEPT_PROFILE_RETRIEVAL_WORKFLOW_IDS
    ):
        target_ids.extend(extract_vontology_concept_ids_from_text(prompt_text))
    return _dedupe_string_sequence(target_ids)


def _apply_target_concepts_to_evidence_contract(
    *,
    contract: Mapping[str, Any] | None,
    target_concept_ids: Sequence[Any],
) -> dict[str, Any] | None:
    if not isinstance(contract, Mapping):
        return None
    targets = _dedupe_string_sequence(target_concept_ids)
    if not targets:
        return dict(contract)

    raw_effects = contract.get("required_effects")
    if not isinstance(raw_effects, Sequence) or isinstance(raw_effects, (str, bytes)):
        return dict(contract)

    contract_payload = dict(contract)
    required_effects: list[Any] = []
    changed = False
    for raw_effect in raw_effects:
        if not isinstance(raw_effect, Mapping):
            required_effects.append(raw_effect)
            continue
        effect = dict(raw_effect)
        existing_targets = _dedupe_string_sequence(effect.get("targets") or [])
        required_tools = _dedupe_string_sequence(effect.get("required_tools") or [])
        targetable = any(_tool_supports_concept_target(tool) for tool in required_tools)
        if (
            targetable
            and not existing_targets
            and _is_evidence_effect_type(_safe_str(effect.get("effect_type")))
        ):
            effect["targets"] = list(targets)
            effect.setdefault(
                "wrong_target_failure_code",
                f"{_effect_failure_code_slug(effect)}_wrong_target",
            )
            changed = True
        required_effects.append(effect)

    if changed:
        contract_payload["required_effects"] = required_effects
        artefact_context = contract_payload.get("artefact_context")
        if isinstance(artefact_context, Mapping):
            contract_payload["artefact_context"] = {
                **dict(artefact_context),
                "target_concept_ids": list(targets),
            }
    return contract_payload


def _build_prompt_required_evidence_contract(
    *,
    prompt_text: Any,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    required_prompt_tools: Sequence[Any] | None = None,
    target_concept_ids: Sequence[Any] | None = None,
) -> dict[str, Any] | None:
    continuation_context = _extract_applied_workflow_continuation_context(aux_llm_calls)
    continuation_contract = _required_effects_contract_from_continuation_context(
        continuation_context,
        intent_class="evidence",
    )
    if isinstance(continuation_contract, Mapping):
        return continuation_contract

    tool_names = _dedupe_string_sequence(
        [
            *(required_prompt_tools or ()),
            *_extract_required_tool_names_from_aux(aux_llm_calls),
        ]
    )
    evidence_tools = [
        tool_name
        for tool_name in tool_names
        if _is_prompt_required_evidence_tool(tool_name)
    ]
    if not evidence_tools:
        return None

    targets = _dedupe_string_sequence(target_concept_ids or [])
    required_effects: list[dict[str, Any]] = []
    for index, tool_name in enumerate(evidence_tools, start=1):
        slug = re.sub(r"[^a-z0-9]+", "_", tool_name.lower()).strip("_") or "tool"
        missing_code = f"prompt_required_evidence_{slug}_missing"
        wrong_target_code = f"prompt_required_evidence_{slug}_wrong_target"
        effect_targets = targets if _tool_supports_concept_target(tool_name) else []
        required_effects.append(
            {
                "effect_id": f"effect_prompt_required_evidence_{slug}_{index}",
                "intent_origin": "prompt_required_effects_contract",
                "effect_type": "required_evidence",
                "description": (
                    f"Observe required evidence retrieval via {tool_name}."
                ),
                "required_tools": [tool_name],
                "required_tools_match": "all",
                "targets": list(effect_targets),
                "required_predicates": [],
                "postcondition_required": True,
                "postcondition_strategy": "execution_observed",
                "status": "not_executed",
                "status_reason": (
                    f"Required evidence tool was not observed: {tool_name}."
                ),
                "missing_failure_code": missing_code,
                "failed_failure_code": f"prompt_required_evidence_{slug}_failed",
                "wrong_target_failure_code": wrong_target_code,
                "failure_code": missing_code,
                "failure_codes": [missing_code],
            }
        )

    prompt_preview = _safe_str(prompt_text)
    return {
        "schema_version": _REPRESENTATION_CONTRACT_SCHEMA_VERSION,
        "contract_id": (
            "prompt_required_evidence_"
            f"{_hash_payload({'required_tools': evidence_tools, 'prompt': prompt_preview}) or 'tools'}"
        ),
        "intent_class": "evidence",
        "domain_profile_id": "prompt_required_evidence",
        "artefact_context": {
            "required_tools": list(evidence_tools),
            "target_concept_ids": list(targets),
            "prompt_preview": prompt_preview[:500] if prompt_preview else "",
        },
        "profile_source": "prompt_tool_requirements",
        "required_effects": required_effects,
    }


def _build_prompt_required_mutation_contract(
    *,
    prompt_text: Any,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    required_prompt_tools: Sequence[Any] | None = None,
) -> dict[str, Any] | None:
    continuation_context = _extract_applied_workflow_continuation_context(aux_llm_calls)
    continuation_contract = _required_effects_contract_from_continuation_context(
        continuation_context,
        intent_class="mutation",
    )
    if isinstance(continuation_contract, Mapping):
        return continuation_contract

    prompt_preview = _safe_str(prompt_text)
    if not prompt_preview:
        return None

    tool_names = _dedupe_string_sequence(
        [
            *(required_prompt_tools or ()),
            *_extract_required_tool_names_from_aux(aux_llm_calls),
        ]
    )
    write_request_evidence = _extract_write_request_evidence_from_aux(aux_llm_calls)
    mutation_tools = [
        tool_name
        for tool_name in tool_names
        if _is_prompt_required_mutation_tool(tool_name)
        and not _write_request_evidence_denies_tool(
            write_request_evidence,
            tool_name,
        )
    ]
    if not mutation_tools:
        return None

    required_effects: list[dict[str, Any]] = []
    for index, tool_name in enumerate(mutation_tools, start=1):
        slug = re.sub(r"[^a-z0-9]+", "_", tool_name.lower()).strip("_") or "tool"
        missing_code = f"prompt_required_mutation_{slug}_missing"
        required_effects.append(
            {
                "effect_id": f"effect_prompt_required_mutation_{slug}_{index}",
                "intent_origin": "prompt_required_effects_contract",
                "effect_type": "required_mutation",
                "description": (
                    f"Observe required mutating tool execution via {tool_name}."
                ),
                "required_tools": [tool_name],
                "required_tools_match": "all",
                "targets": [],
                "required_predicates": [],
                "postcondition_required": True,
                "postcondition_strategy": "execution_observed",
                "status": "not_executed",
                "status_reason": (
                    f"Required mutating tool was not observed: {tool_name}."
                ),
                "missing_failure_code": missing_code,
                "failed_failure_code": f"prompt_required_mutation_{slug}_failed",
                "failure_code": missing_code,
                "failure_codes": [missing_code],
            }
        )

    return {
        "schema_version": _REPRESENTATION_CONTRACT_SCHEMA_VERSION,
        "contract_id": (
            "prompt_required_mutation_"
            f"{_hash_payload({'required_tools': mutation_tools, 'prompt': prompt_preview}) or 'tools'}"
        ),
        "intent_class": "mutation",
        "domain_profile_id": "prompt_required_mutation",
        "artefact_context": {
            "required_tools": list(mutation_tools),
            "prompt_preview": prompt_preview[:500],
        },
        "profile_source": "prompt_tool_requirements",
        "required_effects": required_effects,
    }


def _load_workflow_required_effects_contract(
    *,
    workflow_id: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    workflow_id_value = _safe_str(workflow_id)
    if not workflow_id_value:
        return None, None

    definition = None
    try:
        from ..workflows.durable.registry_factory import (
            get_shared_workflow_registry_read_only,
        )

        registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
        if registry is not None:
            registration = registry.get_registration(workflow_id_value)
            if registration is not None:
                definition = registration.definition
    except Exception:
        logger.debug(
            "workflow required-effects contract load via registry failed",
            exc_info=True,
        )

    if definition is None:
        try:
            from ..workflows.vontology_loader import (
                load_workflow_definition_from_vontology,
            )

            definition = load_workflow_definition_from_vontology(workflow_id_value)
        except Exception:
            logger.debug(
                "workflow required-effects contract load via authoritative definition failed",
                exc_info=True,
            )

    if definition is None:
        return None, None

    metadata_raw = getattr(definition, "metadata", None)
    metadata = dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
    contract = metadata.get("required_effects_contract")
    if not isinstance(contract, Mapping):
        return None, None
    contract_source = _safe_str(metadata.get("required_effects_contract_source"))
    return dict(contract), contract_source


def _workflow_required_effects_contract_from_runtime_surface(
    surface: Mapping[str, Any] | None,
    *,
    source_label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(surface, Mapping):
        return None, None

    contract = surface.get("workflow_required_effects_contract")
    if isinstance(contract, Mapping):
        contract_source = _safe_str(
            surface.get("workflow_required_effects_contract_source")
        )
        return dict(contract), contract_source or source_label

    for nested_key in (
        "workflow_execution_summary",
        "completion_report",
        "child_result_snapshot",
        "result_snapshot",
    ):
        nested = surface.get(nested_key)
        if not isinstance(nested, Mapping):
            continue
        nested_contract, nested_source = (
            _workflow_required_effects_contract_from_runtime_surface(
                nested,
                source_label=f"{source_label}.{nested_key}",
            )
        )
        if isinstance(nested_contract, dict):
            return nested_contract, nested_source

    return None, None


def _resolve_workflow_required_effects_contract(
    *,
    workflow_id: str | None,
    selected_workflow_trace: Mapping[str, Any] | None,
    completion_report: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    for surface, source_label in (
        (selected_workflow_trace, "selected_workflow_trace"),
        (completion_report, "completion_report"),
    ):
        contract, source = _workflow_required_effects_contract_from_runtime_surface(
            surface,
            source_label=source_label,
        )
        if isinstance(contract, dict):
            return contract, source
    return _load_workflow_required_effects_contract(workflow_id=workflow_id)


def _is_representation_effect_type(effect_type: str | None) -> bool:
    if not isinstance(effect_type, str):
        return False
    lowered = effect_type.strip().lower()
    return lowered == "scholarly_representation" or lowered.startswith(
        "representation_"
    )


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


def _normalise_required_tools_match_mode(value: Any, *, default: str = "any") -> str:
    token = (_safe_str(value) or "").lower().replace("-", "_")
    if token in {"all", "every"}:
        return "all"
    if token in {"any", "one", "some"}:
        return "any"
    return default


def _tools_match_mode_satisfied(
    *,
    observed_tools: set[str],
    required_tools: Sequence[str],
    match_mode: str,
) -> bool:
    cleaned_required = [
        _tool_requirement_key(tool)
        for tool in required_tools
        if isinstance(tool, str) and tool.strip()
    ]
    if not cleaned_required:
        return False
    if match_mode == "all":
        return all(tool in observed_tools for tool in cleaned_required)
    return any(tool in observed_tools for tool in cleaned_required)


def _effect_failure_code_slug(effect: Mapping[str, Any]) -> str:
    effect_id = _safe_str(effect.get("effect_id")) or "required_effect"
    slug = re.sub(r"[^a-z0-9]+", "_", effect_id.lower()).strip("_")
    return slug or "required_effect"


def _summarise_required_tool_failure(
    *,
    effect: Mapping[str, Any],
    failed_tools: set[str],
    blocked_tools: set[str],
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> tuple[str, list[str]]:
    required_tools = _dedupe_string_sequence(effect.get("required_tools") or [])
    effect_slug = _effect_failure_code_slug(effect)
    default_failed_code = (
        _safe_str(effect.get("failed_failure_code")) or f"{effect_slug}_failed"
    )
    default_reason = (
        _safe_str(effect.get("not_satisfied_reason"))
        or "Required evidence retrieval failed."
    )

    for tool_name in required_tools:
        canonical_key = _tool_requirement_key(tool_name)
        if canonical_key not in failed_tools and canonical_key not in blocked_tools:
            continue
        for invocation in tool_invocations or ():
            if not isinstance(invocation, Mapping):
                continue
            invocation_tool = _safe_str(invocation.get("tool")) or _safe_str(
                invocation.get("method")
            )
            if _tool_requirement_key(invocation_tool) != canonical_key:
                continue
            payload = _extract_tool_invocation_payload(invocation)
            status = _classify_tool_invocation_status(
                invocation=invocation,
                payload=payload,
            )
            if status == "ok":
                continue
            error_text = _safe_str(invocation.get("error"))
            result_summary = _safe_str(invocation.get("result_summary"))
            payload_summary = (
                _safe_str(payload.get("result_summary"))
                if isinstance(payload, Mapping)
                else None
            ) or (
                _safe_str(payload.get("summary"))
                if isinstance(payload, Mapping)
                else None
            )
            detail = error_text or result_summary or payload_summary
            if status == "blocked":
                detail = detail or f"Required evidence tool was blocked: {tool_name}."
            else:
                detail = detail or f"Required evidence tool failed: {tool_name}."
            detail_lower = detail.lower()
            failure_codes = [default_failed_code]
            if (
                "permission_denied" in detail_lower
                or "permission denied" in detail_lower
            ):
                failure_codes.insert(0, "required_evidence_permission_denied")
            elif "not authorised for conversation" in detail_lower or (
                "not authorized for conversation" in detail_lower
            ):
                failure_codes.insert(0, "required_evidence_permission_denied")
            return detail, _dedupe_string_sequence(failure_codes)
    return default_reason, [default_failed_code]


_REPRESENTATION_EFFECT_PAYLOAD_KEYS: dict[str, str] = {
    "scholarly_representation": "scholarly_representation",
    "representation_person": "person_representation",
    "representation_company": "company_representation",
    "representation_meeting": "meeting_representation",
}


def _extract_tool_invocation_target_ids(invocation: Mapping[str, Any]) -> list[str]:
    payload = _extract_tool_invocation_payload(invocation)
    arguments = invocation.get("effective_arguments")
    if not isinstance(arguments, Mapping):
        raw_arguments = invocation.get("arguments")
        arguments = raw_arguments if isinstance(raw_arguments, Mapping) else None

    return _normalise_representation_target_tokens(
        payload.get("concept_id") if isinstance(payload, Mapping) else None,
        payload.get("instance_of") if isinstance(payload, Mapping) else None,
        payload.get("file_copy_concept_id") if isinstance(payload, Mapping) else None,
        (
            payload.get("computer_file_copy_concept_id")
            if isinstance(payload, Mapping)
            else None
        ),
        payload.get("url") if isinstance(payload, Mapping) else None,
        payload.get("source_url") if isinstance(payload, Mapping) else None,
        payload.get("arxiv_id") if isinstance(payload, Mapping) else None,
        arguments.get("concept_id") if isinstance(arguments, Mapping) else None,
        arguments.get("instance_of") if isinstance(arguments, Mapping) else None,
        (
            arguments.get("file_copy_concept_id")
            if isinstance(arguments, Mapping)
            else None
        ),
        (
            arguments.get("computer_file_copy_concept_id")
            if isinstance(arguments, Mapping)
            else None
        ),
        arguments.get("url") if isinstance(arguments, Mapping) else None,
        arguments.get("source_url") if isinstance(arguments, Mapping) else None,
        arguments.get("arxiv_id") if isinstance(arguments, Mapping) else None,
    )


def _normalise_representation_target_tokens(*raw_values: Any) -> list[str]:
    direct_values = _dedupe_string_sequence(raw_values)
    arxiv_ids = extract_arxiv_id_candidates(*raw_values)
    return _dedupe_string_sequence([*direct_values, *arxiv_ids])


def _resolve_required_effect_source_expression(
    source_expression: str,
    *,
    context_surfaces: Mapping[str, Any] | None,
) -> tuple[bool, Any]:
    expression = _safe_str(source_expression)
    if not expression or not isinstance(context_surfaces, Mapping):
        return False, None

    parts = [part for part in expression.split(".") if part]
    if not parts:
        return False, None

    root = parts[0]
    if root in context_surfaces:
        current: Any = context_surfaces.get(root)
        parts = parts[1:]
    else:
        current = context_surfaces

    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current.get(part)
    return True, current


def _extract_required_effect_targets_with_extractor(
    value: Any,
    *,
    extractor: str | None,
) -> list[str]:
    extractor_name = (_safe_str(extractor) or "identity").lower().replace("-", "_")
    if extractor_name in {"arxiv_id", "arxiv_id_list", "arxiv_ids"}:
        candidates = extract_arxiv_id_candidates(value)
        if extractor_name == "arxiv_id":
            return candidates[:1]
        return candidates
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return _dedupe_string_sequence(value)
    return _normalise_representation_target_tokens(value)


def _lookup_required_effect_context_key_targets(
    context_key: str,
    *,
    context_surfaces: Mapping[str, Any] | None,
    extractor: str | None,
) -> list[str]:
    key = _safe_str(context_key)
    if not key or not isinstance(context_surfaces, Mapping):
        return []

    candidate_values: list[Any] = []
    for surface in context_surfaces.values():
        if not isinstance(surface, Mapping):
            continue
        if key in surface:
            candidate_values.append(surface.get(key))
        for nested_key in (
            "child_result_snapshot",
            "result_snapshot",
            "workflow_execution_summary",
            "workflow_launch_inputs",
            "workflow_continuation_launch_inputs",
        ):
            nested = surface.get(nested_key)
            if isinstance(nested, Mapping) and key in nested:
                candidate_values.append(nested.get(key))
        for contract_key in (
            "expected_outcome_contract_state",
            "expected_outcome_contract",
            "turn_expected_outcome_contract_state",
            "turn_expected_outcome_contract",
        ):
            contract = surface.get(contract_key)
            if isinstance(contract, Mapping):
                if key in contract:
                    candidate_values.append(contract.get(key))
                fields = contract.get("fields")
                if isinstance(fields, Mapping) and key in fields:
                    candidate_values.append(fields.get(key))

    targets: list[str] = []
    for value in candidate_values:
        targets.extend(
            _extract_required_effect_targets_with_extractor(
                value,
                extractor=extractor,
            )
        )
    return _dedupe_string_sequence(targets)


def _dynamic_targets_for_required_effect_template(
    template: Mapping[str, Any],
    *,
    context_surfaces: Mapping[str, Any] | None,
) -> tuple[list[str], bool]:
    source_expressions = _dedupe_string_sequence(
        template.get("targets_source_expressions")
        or template.get("target_source_expressions")
        or []
    )
    context_key = _safe_str(template.get("targets_context_key"))
    extractor = _safe_str(template.get("targets_extractor"))
    dynamic_binding_declared = bool(source_expressions or context_key)
    if not dynamic_binding_declared:
        return [], False

    targets: list[str] = []
    for expression in source_expressions:
        found, value = _resolve_required_effect_source_expression(
            expression,
            context_surfaces=context_surfaces,
        )
        if not found:
            continue
        targets.extend(
            _extract_required_effect_targets_with_extractor(
                value,
                extractor=extractor,
            )
        )
    if context_key:
        targets.extend(
            _lookup_required_effect_context_key_targets(
                context_key,
                context_surfaces=context_surfaces,
                extractor=extractor,
            )
        )
    return _dedupe_string_sequence(targets), True


def _target_slug(target: str, *, index: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", target.lower()).strip("_")
    return slug[:80] or str(index)


def _expand_required_effect_templates_for_dynamic_targets(
    raw_effects: Sequence[Any],
    *,
    context_surfaces: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    expanded: list[Mapping[str, Any]] = []
    for raw_effect in raw_effects:
        if not isinstance(raw_effect, Mapping):
            continue
        targets, dynamic_binding_declared = (
            _dynamic_targets_for_required_effect_template(
                raw_effect,
                context_surfaces=context_surfaces,
            )
        )
        if not dynamic_binding_declared:
            expanded.append(raw_effect)
            continue

        base_effect_id = _safe_str(raw_effect.get("effect_id")) or "required_effect"
        if not targets:
            unresolved_effect = dict(raw_effect)
            unresolved_effect["targets"] = []
            unresolved_effect["target_resolution_failed"] = True
            unresolved_effect["target_resolution_failure_code"] = (
                _safe_str(raw_effect.get("target_resolution_failure_code"))
                or f"{_effect_failure_code_slug(raw_effect)}_target_unresolved"
            )
            expanded.append(unresolved_effect)
            continue

        for index, target in enumerate(targets, start=1):
            effect = dict(raw_effect)
            effect["effect_id"] = (
                f"{base_effect_id}_{index}_{_target_slug(target, index=index)}"
            )
            effect["targets"] = [target]
            effect["dynamic_target"] = target
            expanded.append(effect)
    return expanded


def _collect_successful_tool_payloads(
    *,
    tool_invocations: Sequence[Mapping[str, Any]] | None,
    tool_name: str,
    targets: Sequence[str],
) -> tuple[list[Mapping[str, Any]], int]:
    target_lookup = {
        target.lower() for target in _normalise_representation_target_tokens(*targets)
    }
    matched_payloads: list[Mapping[str, Any]] = []
    successful_count = 0

    for invocation in tool_invocations or ():
        if not isinstance(invocation, Mapping):
            continue
        invocation_name = _safe_str(invocation.get("tool")) or _safe_str(
            invocation.get("method")
        )
        if _tool_requirement_key(invocation_name) != _tool_requirement_key(tool_name):
            continue
        payload = _extract_tool_invocation_payload(invocation)
        if (
            _classify_tool_invocation_status(invocation=invocation, payload=payload)
            != "ok"
        ):
            continue
        successful_count += 1
        if not target_lookup:
            if isinstance(payload, Mapping):
                matched_payloads.append(payload)
            continue
        payload_target_ids = {
            item.lower()
            for item in _extract_tool_invocation_target_ids(invocation)
            if isinstance(item, str) and item.strip()
        }
        if payload_target_ids and payload_target_ids.intersection(target_lookup):
            if isinstance(payload, Mapping):
                matched_payloads.append(payload)

    return matched_payloads, successful_count


def _evaluate_representation_effect_payloads(
    *,
    effect: Mapping[str, Any],
    tool_name: str,
    payloads: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    tool_name_lower = tool_name.lower()
    if tool_name_lower not in {
        "interpret_file_copy",
        "materialise_scholarly_representation_for_file_copy",
        "scholarly_paper.verify_representation",
    }:
        return None

    effect_type = _safe_str(effect.get("effect_type"))
    payload_key = _REPRESENTATION_EFFECT_PAYLOAD_KEYS.get(effect_type or "")
    if not payload_key:
        return None

    domain_id = (
        _safe_str(effect.get("representation_domain_id"))
        or _safe_str(effect.get("domain_profile_id"))
        or (
            "paper"
            if _safe_str(effect.get("effect_type")) == "scholarly_representation"
            else "representation"
        )
    )
    generic_reason = (
        "Required representation tool ran but the requested representation was "
        "not verified."
    )
    required_readback_fields = _required_representation_readback_fields(effect)

    for payload in payloads:
        candidate_payloads: list[Mapping[str, Any]] = []
        representation_payload = payload.get(payload_key)
        if isinstance(representation_payload, Mapping):
            candidate_payloads.append(representation_payload)
        if (
            tool_name_lower == "materialise_scholarly_representation_for_file_copy"
            and effect_type == "scholarly_representation"
        ):
            candidate_payloads.append(payload)
        if (
            tool_name_lower == "scholarly_paper.verify_representation"
            and effect_type == "scholarly_representation"
        ):
            verification_payload = dict(payload)
            if "verified" not in verification_payload:
                verification_payload["verified"] = bool(
                    payload.get("scholarly_representation_verified")
                )
            if "attempted" not in verification_payload:
                verification_payload["attempted"] = True
            if not verification_payload["verified"] and not _safe_str(
                verification_payload.get("reason")
            ):
                failures = _dedupe_string_sequence(
                    verification_payload.get("verification_failures") or []
                )
                if failures:
                    verification_payload["reason"] = (
                        "Scholarly representation verification failed: "
                        + ", ".join(failures)
                        + "."
                    )
            candidate_payloads.append(verification_payload)

        for candidate_payload in candidate_payloads:
            if bool(candidate_payload.get("verified")):
                missing_readback_fields = _missing_representation_readback_fields(
                    candidate_payload,
                    required_fields=required_readback_fields,
                )
                if missing_readback_fields:
                    return {
                        "status": "not_satisfied",
                        "status_reason": (
                            "Required representation tool verified the operation "
                            "but did not return the required read-back artefact IDs: "
                            + ", ".join(missing_readback_fields)
                            + "."
                        ),
                        "failure_codes": [
                            f"{domain_id}_representation_readback_missing"
                        ],
                    }
                return {
                    "status": "satisfied",
                    "status_reason": (
                        _safe_str(candidate_payload.get("reason"))
                        or f"Verified {domain_id} representation from {tool_name_lower}."
                    ),
                    "failure_codes": [],
                }
            if bool(candidate_payload.get("attempted")) or isinstance(
                candidate_payload.get("reason"), str
            ):
                return {
                    "status": "not_satisfied",
                    "status_reason": _safe_str(candidate_payload.get("reason"))
                    or _safe_str(candidate_payload.get("metadata_error"))
                    or generic_reason,
                    "failure_codes": [f"{domain_id}_representation_not_verified"],
                }

    if payloads:
        return {
            "status": "not_satisfied",
            "status_reason": generic_reason,
            "failure_codes": [f"{domain_id}_representation_not_verified"],
        }
    return None


def _required_representation_readback_fields(effect: Mapping[str, Any]) -> list[str]:
    raw_fields = effect.get("required_payload_fields")
    if isinstance(raw_fields, Sequence) and not isinstance(
        raw_fields, (str, bytes, bytearray)
    ):
        fields: list[str] = []
        for field in raw_fields:
            if not isinstance(field, str):
                continue
            field_text = _safe_str(field)
            if isinstance(field_text, str) and field_text:
                fields.append(field_text)
        if fields:
            return fields
    effect_type = _safe_str(effect.get("effect_type"))
    if effect_type == "scholarly_representation":
        return ["paper_concept_id", "file_copy_concept_id"]
    return []


def _payload_has_any_key(payload: Mapping[str, Any], keys: Sequence[str]) -> bool:
    for key in keys:
        if _safe_str(payload.get(key)):
            return True
    return False


def _missing_representation_readback_fields(
    payload: Mapping[str, Any],
    *,
    required_fields: Sequence[str],
) -> list[str]:
    missing: list[str] = []
    for field in required_fields:
        field_name = _safe_str(field)
        if not field_name:
            continue
        if field_name == "file_copy_concept_id":
            if _payload_has_any_key(
                payload,
                (
                    "file_copy_concept_id",
                    "computer_file_copy_concept_id",
                    "source_file_copy_concept_id",
                ),
            ):
                continue
            missing.append(field_name)
            continue
        if not _safe_str(payload.get(field_name)):
            missing.append(field_name)
    return missing


def _materialise_required_effects_from_contract(
    *,
    contract: Mapping[str, Any] | None,
    successful_tools: Sequence[str],
    failed_tools: Sequence[str],
    blocked_tools: Sequence[str],
    tool_invocations: Sequence[Mapping[str, Any]] | None = None,
    context_surfaces: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(contract, Mapping):
        return []
    raw_effects = contract.get("required_effects")
    if not isinstance(raw_effects, Sequence) or isinstance(raw_effects, (str, bytes)):
        return []

    successful_lookup = {
        _tool_requirement_key(name)
        for name in successful_tools
        if isinstance(name, str)
    }
    failed_lookup = {
        _tool_requirement_key(name) for name in failed_tools if isinstance(name, str)
    }
    blocked_lookup = {
        _tool_requirement_key(name) for name in blocked_tools if isinstance(name, str)
    }
    observed_lookup = successful_lookup.union(failed_lookup).union(blocked_lookup)

    contract_schema_version = _safe_str(contract.get("schema_version")) or ""
    contract_intent = (_safe_str(contract.get("intent_class")) or "").lower()
    contract_source = (
        "workflow_required_effects_contract"
        if contract_schema_version == WORKFLOW_REQUIRED_EFFECTS_CONTRACT_SCHEMA_VERSION
        else "required_effects_contract"
    )
    domain_id = _safe_str(contract.get("domain_profile_id")) or (
        "evidence" if contract_intent == "evidence" else "representation"
    )
    required_effects: list[dict[str, Any]] = []
    expanded_effects = _expand_required_effect_templates_for_dynamic_targets(
        raw_effects,
        context_surfaces=context_surfaces,
    )
    for template in expanded_effects:
        if not isinstance(template, Mapping):
            continue
        effect = dict(template)
        required_tools = _dedupe_string_sequence(effect.get("required_tools") or [])
        required_tools_match = _normalise_required_tools_match_mode(
            effect.get("required_tools_match"),
            default="any",
        )
        activation_required_tools = _dedupe_string_sequence(
            effect.get("activation_required_tools") or []
        )
        activation_required_tools_match = _normalise_required_tools_match_mode(
            effect.get("activation_required_tools_match"),
            default="any",
        )
        if activation_required_tools and not _tools_match_mode_satisfied(
            observed_tools=observed_lookup,
            required_tools=activation_required_tools,
            match_mode=activation_required_tools_match,
        ):
            continue

        existing_status = (_safe_str(effect.get("status")) or "").lower()
        effect_status = (
            existing_status
            if existing_status in {"satisfied", "not_satisfied", "not_executed"}
            else "not_executed"
        )
        default_not_executed_reason = (
            _safe_str(effect.get("not_executed_reason"))
            or _safe_str(effect.get("status_reason"))
            or (
                "No required representation tool execution was observed."
                if contract_intent == "representation"
                or _is_representation_effect_type(_safe_str(effect.get("effect_type")))
                else (
                    "Required evidence was not retrieved."
                    if _is_evidence_effect_type(_safe_str(effect.get("effect_type")))
                    else "Required effect was not observed."
                )
            )
        )
        status_reason = default_not_executed_reason
        failure_codes = _normalise_failure_codes(effect.get("failure_codes"))
        explicit_failure_code = _safe_str(effect.get("failure_code"))
        if explicit_failure_code and explicit_failure_code not in failure_codes:
            failure_codes.append(explicit_failure_code)

        targets = _dedupe_string_sequence(effect.get("targets") or [])
        effect_slug = _effect_failure_code_slug(effect)
        if bool(effect.get("target_resolution_failed")):
            target_failure_code = (
                _safe_str(effect.get("target_resolution_failure_code"))
                or f"{effect_slug}_target_unresolved"
            )
            effect["status"] = "not_executed"
            effect["status_reason"] = (
                _safe_str(effect.get("target_resolution_failure_reason"))
                or "Required effect targets could not be resolved from the workflow-authored contract."
            )
            effect["failure_code"] = target_failure_code
            effect["failure_codes"] = [target_failure_code]
            if contract_source == "workflow_required_effects_contract":
                effect.setdefault("intent_origin", "workflow_authored")
            effect.setdefault("source", contract_source)
            required_effects.append(effect)
            continue
        is_representation_effect = contract_intent == "representation" or (
            _is_representation_effect_type(_safe_str(effect.get("effect_type")))
        )
        is_evidence_effect = contract_intent == "evidence" or _is_evidence_effect_type(
            _safe_str(effect.get("effect_type"))
        )
        if not required_tools:
            if (
                _safe_str(effect.get("effect_type")) or ""
            ) != "representation_contract_guard":
                continue
            effect["status"] = effect_status
            effect["status_reason"] = status_reason
            effect.setdefault("source", contract_source)
            if contract_source == "workflow_required_effects_contract":
                effect.setdefault("intent_origin", "workflow_authored")
            if failure_codes:
                effect["failure_code"] = failure_codes[0]
                effect["failure_codes"] = failure_codes
            else:
                effect.pop("failure_code", None)
                effect["failure_codes"] = []
            required_effects.append(effect)
            continue
        default_missing_failure_code = _safe_str(
            effect.get("missing_failure_code")
        ) or (
            f"{domain_id}_representation_not_executed"
            if is_representation_effect
            else f"{effect_slug}_not_executed"
        )
        first_success_tool: str | None = None
        matching_success_payloads: list[Mapping[str, Any]] = []
        successful_other_target = False
        successful_required_tools: list[str] = []
        for tool_name in required_tools:
            if _tool_requirement_key(tool_name) not in successful_lookup:
                continue
            payloads, successful_count = _collect_successful_tool_payloads(
                tool_invocations=tool_invocations,
                tool_name=tool_name,
                targets=targets,
            )
            if payloads or not targets:
                successful_required_tools.append(tool_name)
                if first_success_tool is None:
                    first_success_tool = tool_name
                    matching_success_payloads = payloads
            elif successful_count > 0:
                successful_other_target = True

        if required_tools_match == "all":
            success_requirement_met = len(successful_required_tools) == len(
                required_tools
            )
        else:
            success_requirement_met = bool(successful_required_tools)

        if success_requirement_met and first_success_tool:
            payload_verdict = _evaluate_representation_effect_payloads(
                effect=effect,
                tool_name=first_success_tool,
                payloads=matching_success_payloads,
            )
            if is_representation_effect and isinstance(payload_verdict, Mapping):
                effect_status = (
                    _safe_str(payload_verdict.get("status")) or "not_satisfied"
                )
                status_reason = _safe_str(payload_verdict.get("status_reason")) or (
                    "Representation payload verification failed."
                )
                failure_codes = _normalise_failure_codes(
                    payload_verdict.get("failure_codes")
                )
            else:
                effect_status = "satisfied"
                status_reason = (
                    "Observed required "
                    f"{'representation tool' if is_representation_effect else 'tool'} invocation: "
                    f"{first_success_tool}."
                )
                failure_codes = []
        else:
            has_failed_required_tool = any(
                _tool_requirement_key(tool) in failed_lookup
                or _tool_requirement_key(tool) in blocked_lookup
                for tool in required_tools
            )
            if has_failed_required_tool:
                effect_status = "not_satisfied"
                status_reason, failure_codes = _summarise_required_tool_failure(
                    effect=effect,
                    failed_tools=failed_lookup,
                    blocked_tools=blocked_lookup,
                    tool_invocations=tool_invocations,
                )
            elif successful_other_target and (
                is_representation_effect or is_evidence_effect
            ):
                effect_status = "not_executed"
                status_reason = _safe_str(effect.get("wrong_target_reason")) or (
                    "Required "
                    f"{'representation' if is_representation_effect else 'evidence'} "
                    "tool ran, but not for the required target."
                )
                failure_codes = [
                    _safe_str(effect.get("wrong_target_failure_code"))
                    or (
                        f"{domain_id}_representation_wrong_target"
                        if is_representation_effect
                        else f"{effect_slug}_wrong_target"
                    )
                ]
            else:
                effect_status = "not_executed"
                status_reason = default_not_executed_reason
                failure_codes = [default_missing_failure_code]

        effect["status"] = effect_status
        effect["status_reason"] = status_reason
        if contract_source == "workflow_required_effects_contract":
            effect.setdefault("intent_origin", "workflow_authored")
        effect.setdefault("source", contract_source)
        if failure_codes:
            effect["failure_code"] = failure_codes[0]
            effect["failure_codes"] = failure_codes
        else:
            effect.pop("failure_code", None)
            effect["failure_codes"] = []

        required_effects.append(effect)

    return required_effects


# ---------------------------------------------------------------------------
# Tool-authored mutation effects
# ---------------------------------------------------------------------------
# When write tool invocations carry structured arguments (concept_id,
# predicate_concept_id, etc.), we build mutation effects that are
# *tool-authored* — derived from the tool's own payload/arguments rather
# than from a coarse observation that "some write tool ran".  This keeps
# completion semantics grounded in tool/workflow evidence rather than
# Python-authored generic mutation inference.
# ---------------------------------------------------------------------------


def _extract_mutation_metadata_from_invocation(
    invocation: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Extract mutation-relevant metadata from a single write tool invocation."""
    tool_name = _safe_str(invocation.get("tool")) or _safe_str(invocation.get("method"))
    if not tool_name or not _is_write_tool(tool_name):
        return None

    payload = _extract_tool_invocation_payload(invocation)
    status = _classify_tool_invocation_status(invocation=invocation, payload=payload)
    targets = _extract_tool_invocation_target_ids(invocation)

    predicates: list[str] = []
    for source in (
        invocation.get("effective_arguments"),
        invocation.get("arguments"),
        payload,
    ):
        if not isinstance(source, Mapping):
            continue
        for key in ("predicate_concept_id", "predicate"):
            val = _safe_str(source.get(key))
            if val and val not in predicates:
                predicates.append(val)

    return {
        "tool_name": tool_name,
        "status": status,
        "targets": targets,
        "predicates": predicates,
        "payload": payload if isinstance(payload, Mapping) else None,
    }


def _build_tool_authored_mutation_effects(
    *,
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Build mutation effects from tool-authored invocation metadata.

    Groups write tool invocations by tool name, collecting targets and
    predicates from each invocation's arguments/payload.  Each group
    yields one effect with ``intent_origin: "tool_authored"``.
    """
    if not tool_invocations:
        return []

    groups: dict[str, dict[str, Any]] = {}
    tool_order: list[str] = []

    for invocation in tool_invocations:
        if not isinstance(invocation, Mapping):
            continue
        metadata = _extract_mutation_metadata_from_invocation(invocation)
        if metadata is None:
            continue

        canonical_key = _tool_requirement_key(metadata["tool_name"])
        if canonical_key not in groups:
            groups[canonical_key] = {
                "tool_name": metadata["tool_name"],
                "targets": [],
                "predicates": [],
                "any_success": False,
                "any_failure": False,
                "any_blocked": False,
                "payloads": [],
            }
            tool_order.append(canonical_key)

        group = groups[canonical_key]
        for t in metadata["targets"]:
            if t not in group["targets"]:
                group["targets"].append(t)
        for p in metadata["predicates"]:
            if p not in group["predicates"]:
                group["predicates"].append(p)
        payload = metadata.get("payload")
        if isinstance(payload, Mapping):
            group["payloads"].append(payload)

        if metadata["status"] == "ok":
            group["any_success"] = True
        elif metadata["status"] == "blocked":
            group["any_blocked"] = True
        else:
            group["any_failure"] = True

    effects: list[dict[str, Any]] = []
    for index, lowered in enumerate(tool_order):
        group = groups[lowered]
        tool_name = group["tool_name"]
        targets = group["targets"][:5]
        predicates = group["predicates"][:5]

        if group["any_success"]:
            effect_status = "satisfied"
            status_reason = f"Successful {tool_name} invocation observed."
        elif group["any_blocked"]:
            effect_status = "not_satisfied"
            status_reason = f"{tool_name} invocation was blocked."
        elif group["any_failure"]:
            effect_status = "not_satisfied"
            status_reason = f"{tool_name} invocation failed."
        else:
            effect_status = "not_executed"
            status_reason = f"No {tool_name} invocation completed."

        failure_codes: list[str] = []
        if effect_status == "not_satisfied":
            failure_codes = [f"kb_mutation_{tool_name.lower()}_failed"]

        target_detail = f" targeting {', '.join(targets[:2])}" if targets else ""
        predicate_detail = f" ({', '.join(predicates[:2])})" if predicates else ""

        effect: dict[str, Any] = {
            "effect_id": f"mutation_{index + 1}",
            "intent_origin": "tool_authored",
            "effect_type": "kb_mutation",
            "description": (
                f"Mutation via {tool_name}{target_detail}{predicate_detail}."
            ),
            "required_tools": [tool_name],
            "targets": targets,
            "required_predicates": predicates,
            "postcondition_required": True,
            "postcondition_strategy": "state_requery",
            "status": effect_status,
            "status_reason": status_reason,
            "failure_codes": failure_codes,
        }
        if failure_codes:
            effect["failure_code"] = failure_codes[0]

        if tool_name.lower() == "materialise_scholarly_representation_for_file_copy":
            effect.update(
                {
                    "effect_id": f"effect_paper_representation_tool_{index + 1}",
                    "effect_type": "scholarly_representation",
                    "representation_domain_id": "paper",
                    "description": (
                        "Materialise scholarly paper representation from file-copy context."
                    ),
                    "required_predicates": [
                        "#V#computer_file_for_propositional_information_thing",
                        "#V#propositional_information_thing_has_computer_file",
                    ],
                    "postcondition_strategy": "execution_observed",
                }
            )
            payload_verdict = _evaluate_representation_effect_payloads(
                effect=effect,
                tool_name=tool_name,
                payloads=group["payloads"],
            )
            if isinstance(payload_verdict, Mapping):
                effect["status"] = (
                    _safe_str(payload_verdict.get("status")) or "not_satisfied"
                )
                effect["status_reason"] = (
                    _safe_str(payload_verdict.get("status_reason"))
                    or "Representation payload verification failed."
                )
                effect["failure_codes"] = _normalise_failure_codes(
                    payload_verdict.get("failure_codes")
                )
                if effect["failure_codes"]:
                    effect["failure_code"] = effect["failure_codes"][0]
                else:
                    effect.pop("failure_code", None)

        effects.append(effect)

    return effects


def _build_postcondition_checks(
    *,
    required_effects: Sequence[Mapping[str, Any]],
    successful_write_tools: Sequence[str],
    successful_verification_tools: Sequence[str],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for effect in required_effects:
        effect_id = _safe_str(effect.get("effect_id")) or "effect_1"
        effect_status = _safe_str(effect.get("status")) or "pending"
        effect_type = _safe_str(effect.get("effect_type")) or "kb_mutation"
        postcondition_strategy = (
            _safe_str(effect.get("postcondition_strategy")) or ""
        ).lower()
        if effect_type == "tool_execution":
            if effect_status == "satisfied":
                check_status = "verified"
                evidence = "Required tool execution was observed."
                verification_mode = "execution_observed"
            elif effect_status in {"not_satisfied", "not_executed"}:
                check_status = "not_verified"
                evidence = (
                    _safe_str(effect.get("status_reason"))
                    or "Required tool execution was not observed."
                )
                verification_mode = "execution_missing"
            else:
                check_status = "inconclusive"
                evidence = "Tool execution verification outcome is inconclusive."
                verification_mode = "execution_inconclusive"
        elif effect_type == "workflow_execution":
            if effect_status == "satisfied":
                check_status = "verified"
                evidence = "Required workflow execution was observed."
                verification_mode = "workflow_terminal_success"
            elif effect_status in {"not_satisfied", "not_executed"}:
                check_status = "not_verified"
                evidence = (
                    _safe_str(effect.get("status_reason"))
                    or _CUSTOM_WORKFLOW_EXECUTION_FAILURE_REASON
                )
                verification_mode = "workflow_terminal_failed"
            else:
                check_status = "inconclusive"
                evidence = "Workflow execution verification outcome is inconclusive."
                verification_mode = "workflow_terminal_inconclusive"
        elif _is_representation_effect_type(effect_type):
            representation_label = (
                "Scholarly paper representation"
                if effect_type == "scholarly_representation"
                else "Requested representation"
            )
            if effect_status == "satisfied":
                check_status = "verified"
                evidence = (
                    _safe_str(effect.get("status_reason"))
                    or f"{representation_label} execution was observed."
                )
                verification_mode = "execution_observed"
            elif effect_status in {"not_satisfied", "not_executed"}:
                check_status = "not_verified"
                evidence = (
                    _safe_str(effect.get("status_reason"))
                    or f"{representation_label} execution was not observed."
                )
                verification_mode = "execution_missing"
            else:
                check_status = "inconclusive"
                evidence = f"{representation_label} verification is inconclusive."
                verification_mode = "execution_inconclusive"
        else:
            if postcondition_strategy == "execution_observed":
                if effect_status == "satisfied":
                    check_status = "verified"
                    evidence = (
                        _safe_str(effect.get("status_reason"))
                        or "Required effect execution was observed."
                    )
                    verification_mode = "execution_observed"
                elif effect_status in {"not_satisfied", "not_executed"}:
                    check_status = "not_verified"
                    evidence = (
                        _safe_str(effect.get("status_reason"))
                        or "Required effect execution was not observed."
                    )
                    verification_mode = "execution_missing"
                else:
                    check_status = "inconclusive"
                    evidence = "Execution-observed verification is inconclusive."
                    verification_mode = "execution_inconclusive"
            elif effect_status == "satisfied" and successful_write_tools:
                if successful_verification_tools:
                    check_status = "verified"
                    evidence = (
                        "Observed postcondition verification read/check tool(s): "
                        + ", ".join(successful_verification_tools[:3])
                    )
                    verification_mode = "state_requery_observed"
                else:
                    check_status = "inconclusive"
                    evidence = (
                        "Write tool invocation succeeded but explicit state "
                        "re-query/check tool invocation was not observed."
                    )
                    verification_mode = "state_requery_missing"
            elif effect_status in {"not_satisfied", "not_executed"}:
                check_status = "not_verified"
                evidence = "Required mutation effect is unresolved."
                verification_mode = "state_requery_blocked"
            else:
                check_status = "inconclusive"
                evidence = "Mutation verification outcome is inconclusive."
                verification_mode = "state_requery_inconclusive"

        checks.append(
            {
                "check_id": f"check_{effect_id}",
                "effect_id": effect_id,
                "check_type": (
                    "tool_execution_observed"
                    if effect_type == "tool_execution"
                    else (
                        "workflow_execution_observed"
                        if effect_type == "workflow_execution"
                        else (
                            "scholarly_representation_observed"
                            if _is_representation_effect_type(effect_type)
                            else (
                                "effect_execution_observed"
                                if postcondition_strategy == "execution_observed"
                                else "predicate_exists"
                            )
                        )
                    )
                ),
                "check_tool": "derived.turn_execution",
                "check_payload": {},
                "observed": {
                    "successful_write_tools": list(successful_write_tools),
                    "successful_verification_tools": list(
                        successful_verification_tools
                    ),
                    "effect_status": effect_status,
                    "effect_type": effect_type,
                },
                "status": check_status,
                "verification_mode": verification_mode,
                "evidence": evidence,
                "error": None,
            }
        )
    return checks


def _summarise_check_counts(
    checks: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    summary = {
        "verified_count": 0,
        "not_verified_count": 0,
        "inconclusive_count": 0,
        "error_count": 0,
    }
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        status = (_safe_str(check.get("status")) or "").lower()
        if status == "verified":
            summary["verified_count"] += 1
        elif status == "not_verified":
            summary["not_verified_count"] += 1
        elif status == "inconclusive":
            summary["inconclusive_count"] += 1
        elif status == "error":
            summary["error_count"] += 1
    return summary


def _derive_completion_gate(
    *,
    required_effects: Sequence[Mapping[str, Any]],
    postcondition_checks: Sequence[Mapping[str, Any]],
    completion_claim_validated: bool,
    execution_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    decision = "completed"
    decision_reason = "No blocking effect detected."
    blocking_effect_ids: list[str] = []
    blocking_failure_codes: list[str] = []

    unresolved_effect_ids: list[str] = []
    unresolved_effect_types: set[str] = set()
    unresolved_failure_codes: list[str] = []
    for effect in required_effects:
        effect_status = _safe_str(effect.get("status")) or ""
        effect_id = _safe_str(effect.get("effect_id")) or "effect_1"
        if effect_status in {"not_satisfied", "not_executed"}:
            unresolved_effect_ids.append(effect_id)
            effect_type = _safe_str(effect.get("effect_type"))
            if effect_type:
                unresolved_effect_types.add(effect_type)
            unresolved_failure_codes.extend(
                _normalise_failure_codes(effect.get("failure_codes"))
            )
            single_failure_code = _safe_str(effect.get("failure_code"))
            if single_failure_code:
                unresolved_failure_codes.append(single_failure_code)

    unresolved_check_ids: list[str] = []
    verified_check_effect_ids: list[str] = []
    unresolved_postcondition_checks: list[dict[str, Any]] = []
    for check in postcondition_checks:
        check_status = _safe_str(check.get("status")) or ""
        check_effect_id = _safe_str(check.get("effect_id")) or "effect_1"
        if check_status == "verified":
            verified_check_effect_ids.append(check_effect_id)
            continue
        if check_status in {"not_verified", "inconclusive", "error"}:
            unresolved_check_ids.append(check_effect_id)
            unresolved_postcondition_checks.append(
                {
                    "effect_id": check_effect_id,
                    "check_id": _safe_str(check.get("check_id")),
                    "check_type": _safe_str(check.get("check_type")),
                    "status": check_status,
                    "verification_mode": _safe_str(check.get("verification_mode")),
                    "evidence": _safe_str(check.get("evidence")),
                }
            )

    unresolved_preconditions: list[dict[str, Any]] = []
    for effect in required_effects:
        effect_status = _safe_str(effect.get("status")) or ""
        if effect_status not in {"not_satisfied", "not_executed"}:
            continue
        failure_codes = _normalise_failure_codes(effect.get("failure_codes"))
        single_failure_code = _safe_str(effect.get("failure_code"))
        if single_failure_code and single_failure_code not in failure_codes:
            failure_codes.append(single_failure_code)
        unresolved_preconditions.append(
            {
                "effect_id": _safe_str(effect.get("effect_id")) or "effect_1",
                "effect_type": _safe_str(effect.get("effect_type")) or "kb_mutation",
                "status": effect_status,
                "status_reason": _safe_str(effect.get("status_reason")),
                "failure_codes": failure_codes,
            }
        )

    if unresolved_effect_ids:
        blocking_effect_ids = sorted(set(unresolved_effect_ids))
        if unresolved_failure_codes:
            blocking_failure_codes = sorted(
                {
                    code
                    for code in unresolved_failure_codes
                    if isinstance(code, str) and code.strip()
                }
            )
        else:
            unresolved_statuses = {
                _safe_str(entry.get("status")) or ""
                for entry in unresolved_preconditions
                if isinstance(entry, Mapping)
            }
            fallback_codes: list[str] = ["required_effects_unresolved"]
            if "not_satisfied" in unresolved_statuses:
                fallback_codes.append("required_effect_not_satisfied")
            if "not_executed" in unresolved_statuses:
                fallback_codes.append("required_effect_not_executed")
            blocking_failure_codes = sorted(set(fallback_codes))
        if any(
            (_safe_str(effect.get("status")) or "") == "not_satisfied"
            for effect in required_effects
        ):
            decision = "failed"
            first_evidence_status_reason = next(
                (
                    _safe_str(entry.get("status_reason"))
                    for entry in unresolved_preconditions
                    if _is_evidence_effect_type(_safe_str(entry.get("effect_type")))
                    and _safe_str(entry.get("status_reason"))
                ),
                None,
            )
            if unresolved_effect_types and all(
                _is_evidence_effect_type(effect_type)
                for effect_type in unresolved_effect_types
            ):
                decision_reason = (
                    first_evidence_status_reason
                    or "Required evidence retrieval failed."
                )
            else:
                decision_reason = "Mutation attempt failed or was blocked."
        else:
            decision = "escalation_required"
            representation_failure_reason = next(
                (
                    _REPRESENTATION_FAILURE_REASON_MAP.get(code)
                    for code in blocking_failure_codes
                    if isinstance(code, str)
                    and code in _REPRESENTATION_FAILURE_REASON_MAP
                ),
                None,
            )
            first_representation_status_reason = next(
                (
                    _safe_str(entry.get("status_reason"))
                    for entry in unresolved_preconditions
                    if _is_representation_effect_type(
                        _safe_str(entry.get("effect_type"))
                    )
                    and _safe_str(entry.get("status_reason"))
                ),
                None,
            )
            first_evidence_status_reason = next(
                (
                    _safe_str(entry.get("status_reason"))
                    for entry in unresolved_preconditions
                    if _is_evidence_effect_type(_safe_str(entry.get("effect_type")))
                    and _safe_str(entry.get("status_reason"))
                ),
                None,
            )
            if unresolved_effect_types == {"tool_execution"}:
                decision_reason = "Required tool execution was not observed."
            elif unresolved_effect_types == {"workflow_execution"}:
                first_workflow_status_reason = next(
                    (
                        _safe_str(entry.get("status_reason"))
                        for entry in unresolved_preconditions
                        if (_safe_str(entry.get("effect_type")) or "")
                        == "workflow_execution"
                        and _safe_str(entry.get("status_reason"))
                    ),
                    None,
                )
                decision_reason = (
                    first_workflow_status_reason
                    or _CUSTOM_WORKFLOW_EXECUTION_FAILURE_REASON
                )
            elif (
                unresolved_effect_types == {"scholarly_representation"}
                and not representation_failure_reason
            ):
                decision_reason = (
                    first_representation_status_reason
                    or "Required scholarly paper representation was not executed."
                )
            elif unresolved_effect_types and all(
                _is_representation_effect_type(effect_type)
                for effect_type in unresolved_effect_types
            ):
                decision_reason = (
                    first_representation_status_reason
                    or representation_failure_reason
                    or "Required representation was not executed."
                )
            elif unresolved_effect_types and all(
                _is_evidence_effect_type(effect_type)
                for effect_type in unresolved_effect_types
            ):
                decision_reason = (
                    first_evidence_status_reason
                    or "Required evidence was not retrieved."
                )
            else:
                decision_reason = "Required mutation was not executed."
    elif unresolved_check_ids:
        blocking_effect_ids = sorted(set(unresolved_check_ids))
        decision = "partial"
        decision_reason = "Mutation execution observed but postcondition verification is inconclusive."
        if not blocking_failure_codes:
            blocking_failure_codes = ["postcondition_inconclusive"]

    execution_signal_blocker = None
    repeat_eligible = True
    if decision == "completed":
        execution_signal_blocker = _derive_execution_signal_completion_blocker(
            execution_summary=execution_summary
        )
        if isinstance(execution_signal_blocker, Mapping):
            blocking_effect_ids = [
                _safe_str(execution_signal_blocker.get("effect_id"))
                or "effect_execution_signal_1"
            ]
            blocking_failure_codes = _normalise_failure_codes(
                execution_signal_blocker.get("failure_codes")
            )
            single_failure_code = _safe_str(
                execution_signal_blocker.get("failure_code")
            )
            if (
                single_failure_code
                and single_failure_code not in blocking_failure_codes
            ):
                blocking_failure_codes.append(single_failure_code)
            decision = (
                _safe_str(execution_signal_blocker.get("decision"))
                or "escalation_required"
            )
            decision_reason = (
                _safe_str(execution_signal_blocker.get("decision_reason"))
                or "Execution evidence does not support a completed verdict."
            )
            repeat_eligible = bool(
                execution_signal_blocker.get("repeat_eligible", False)
            )
            unresolved_preconditions.append(
                {
                    "effect_id": _safe_str(execution_signal_blocker.get("effect_id"))
                    or "effect_execution_signal_1",
                    "effect_type": _safe_str(
                        execution_signal_blocker.get("effect_type")
                    )
                    or "tool_execution",
                    "status": _safe_str(execution_signal_blocker.get("status"))
                    or "not_executed",
                    "status_reason": _safe_str(
                        execution_signal_blocker.get("status_reason")
                    ),
                    "failure_codes": blocking_failure_codes,
                }
            )

    if decision == "completed" and not completion_claim_validated:
        decision = "partial"
        decision_reason = (
            "Completion claim was detected but could not be fully validated."
        )
        if not blocking_failure_codes:
            blocking_failure_codes = ["completion_claim_unvalidated"]

    safe_to_claim = decision == "completed"
    evidence_payload = {
        "evaluation_basis": (
            "required_effects_postcondition_checks_and_execution_signals"
            if execution_signal_blocker
            else "required_effects_and_postcondition_checks"
        ),
        "required_effect_count": len(required_effects),
        "postcondition_check_count": len(postcondition_checks),
        "postcondition_summary": _summarise_check_counts(postcondition_checks),
        "verified_effect_ids": sorted(set(verified_check_effect_ids)),
        "unresolved_effect_ids": sorted(set(unresolved_check_ids)),
        "unresolved_preconditions": unresolved_preconditions,
        "unresolved_postcondition_checks": unresolved_postcondition_checks,
        "execution_signal_blocker": (
            dict(execution_signal_blocker)
            if isinstance(execution_signal_blocker, Mapping)
            else None
        ),
        "repeat_eligible": repeat_eligible,
        "completion_outcome": (
            "success"
            if decision == "completed"
            else "inconclusive" if decision == "partial" else "failure"
        ),
    }
    return {
        "workflow_id": "#V#turn_completion_gate_workflow",
        "decision": decision,
        "decision_reason": decision_reason,
        "blocking_effect_ids": blocking_effect_ids,
        "blocking_failure_codes": blocking_failure_codes,
        "safe_to_claim_completion": safe_to_claim,
        "requires_follow_up": not safe_to_claim,
        "repeat_eligible": repeat_eligible,
        "evidence_payload": evidence_payload,
    }


def build_turn_execution_record(
    *,
    request_id: Any,
    session_id: Any,
    namespace: Any,
    actor_concept_id: Any = None,
    user_id: Any,
    org_id: Any,
    prompt_text: Any,
    response_text: Any,
    interaction_timestamp_utc: Any,
    workflow_discovery: Mapping[str, Any] | None = None,
    workflow_routing: Mapping[str, Any] | None = None,
    tool_invocations: Sequence[Mapping[str, Any]] | None = None,
    search_evidence: Sequence[Mapping[str, Any]] | None = None,
    turn_execution_diagnostics: Mapping[str, Any] | None = None,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None = None,
    llm_calls: Sequence[Mapping[str, Any]] | None = None,
    selected_workflow_trace: Mapping[str, Any] | None = None,
    turn_expected_outcome_contract: Mapping[str, Any] | None = None,
    critic_verdict: Mapping[str, Any] | None = None,
    completion_gate_verdict: Mapping[str, Any] | None = None,
    completion_report: Mapping[str, Any] | None = None,
    required_prompt_tools: Sequence[Any] | None = None,
    required_tool_obligation_ledger: Mapping[str, Any] | None = None,
    tool_observation_ledger: Mapping[str, Any] | None = None,
    method_catalogue: Mapping[str, Any] | None = None,
    workflow_failure_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_actor_concept_id, actor_identity_source = _resolve_actor_concept_identity(
        actor_concept_id=actor_concept_id,
        user_id=user_id,
        namespace=namespace,
    )
    workflow_discovery_normalised = _extract_workflow_discovery(workflow_discovery)
    final_answer_synthesis = _build_final_answer_synthesis_telemetry(
        aux_llm_calls=aux_llm_calls,
        llm_calls=llm_calls,
    )
    selected_workflow_id = None
    selector_verdict = None
    selector_source = "default"

    if isinstance(workflow_routing, Mapping):
        selected_workflow_id = _safe_str(workflow_routing.get("workflow_id"))
        selector_verdict = _safe_str(workflow_routing.get("verdict"))
        raw_source = (_safe_str(workflow_routing.get("source")) or "default").lower()
        selector_source = (
            "workflow_selector" if raw_source == "selector" else raw_source
        )

    (
        serialised_invocations,
        successful_tools,
        failed_tools,
        blocked_tools,
        successful_write_tools,
        successful_verification_tools,
    ) = _summarise_tool_invocations(tool_invocations)
    search_evidence_payload = [
        dict(item) for item in (search_evidence or ()) if isinstance(item, Mapping)
    ]
    if not search_evidence_payload:
        search_evidence_payload = build_search_tool_evidence(tool_invocations)
    tool_observation_ledger_payload = build_tool_observation_ledger(
        tool_invocations=tool_invocations,
        turn_execution_diagnostics=(
            turn_execution_diagnostics
            if isinstance(turn_execution_diagnostics, Mapping)
            else None
        ),
        aux_llm_calls=aux_llm_calls,
        existing_ledger=(
            tool_observation_ledger
            if isinstance(tool_observation_ledger, Mapping)
            else None
        ),
    )
    resolved_turn_expected_outcome_contract = (
        _resolve_turn_expected_outcome_contract_snapshot(
            turn_expected_outcome_contract,
            selected_workflow_trace,
            workflow_discovery,
            completion_report,
        )
    )
    obligation_carry_forward_adjudication = build_turn_context_adjudication_projection(
        (
            ("aux_llm_calls", {"aux_llm_calls": list(aux_llm_calls or ())}),
            (
                "turn_execution_diagnostics",
                (
                    turn_execution_diagnostics
                    if isinstance(turn_execution_diagnostics, Mapping)
                    else None
                ),
            ),
            (
                "selected_workflow_trace",
                (
                    selected_workflow_trace
                    if isinstance(selected_workflow_trace, Mapping)
                    else None
                ),
            ),
            (
                "completion_report",
                completion_report if isinstance(completion_report, Mapping) else None,
            ),
        )
    )
    obligation_carry_forward_permitted = obligation_carry_forward_permission(
        obligation_carry_forward_adjudication
    )
    (
        activated_conditional_required_tools,
        obligation_carry_forward_projection,
    ) = _activated_turn_expected_conditional_required_tools(
        contract=resolved_turn_expected_outcome_contract,
        tool_invocations=tool_invocations,
        obligation_carry_forward_permitted=obligation_carry_forward_permitted,
    )
    turn_expected_outcome_contract_payload = (
        resolved_turn_expected_outcome_contract.to_dict()
    )
    turn_expected_outcome_contract_available = (
        not resolved_turn_expected_outcome_contract.is_empty()
    )
    selected_workflow_trace_payload = (
        {
            str(key): value
            for key, value in selected_workflow_trace.items()
            if isinstance(key, str)
        }
        if isinstance(selected_workflow_trace, Mapping)
        else None
    )
    if (
        selected_workflow_trace_payload is not None
        and turn_expected_outcome_contract_available
    ):
        selected_workflow_trace_payload["expected_outcome_contract"] = dict(
            turn_expected_outcome_contract_payload
        )
        selected_workflow_trace_payload["expected_outcome_contract_state"] = (
            resolved_turn_expected_outcome_contract.to_state_payload()
        )
    completion_report_payload = (
        dict(completion_report) if isinstance(completion_report, Mapping) else None
    )
    workflow_required_effect_context_surfaces = {
        "selected_workflow_trace": (
            selected_workflow_trace_payload
            if isinstance(selected_workflow_trace_payload, Mapping)
            else {}
        ),
        "completion_report": (
            completion_report_payload
            if isinstance(completion_report_payload, Mapping)
            else {}
        ),
        "turn_expected_outcome_contract": dict(turn_expected_outcome_contract_payload),
        "turn_expected_outcome_contract_state": (
            resolved_turn_expected_outcome_contract.to_state_payload()
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
    }
    required_evidence_target_concept_ids = _derive_required_evidence_target_concept_ids(
        prompt_text=prompt_text,
        selected_workflow_id=selected_workflow_id,
        resolved_turn_expected_outcome_contract=resolved_turn_expected_outcome_contract,
        selected_workflow_trace=selected_workflow_trace_payload,
        completion_report=completion_report_payload,
    )
    effective_required_prompt_tools = _dedupe_string_sequence(
        [
            *(required_prompt_tools or ()),
            *resolved_turn_expected_outcome_contract.required_tools,
            *activated_conditional_required_tools,
            *_extract_string_sequence_from_mapping(
                selected_workflow_trace_payload,
                "required_prompt_tools",
            ),
            *_extract_string_sequence_from_mapping(
                completion_report_payload,
                "required_prompt_tools",
            ),
        ]
    )
    required_tool_sources = {
        "required_prompt_tools": list(required_prompt_tools or ()),
        "turn_expected_outcome_contract": list(
            resolved_turn_expected_outcome_contract.required_tools
        ),
        "turn_expected_outcome_conditional_required_tools": list(
            activated_conditional_required_tools
        ),
        "selected_workflow_trace_required_prompt_tools": (
            _extract_string_sequence_from_mapping(
                selected_workflow_trace_payload,
                "required_prompt_tools",
            )
        ),
        "completion_report_required_prompt_tools": (
            _extract_string_sequence_from_mapping(
                completion_report_payload,
                "required_prompt_tools",
            )
        ),
    }
    existing_required_tool_obligation_ledger = _extract_required_tool_obligation_ledger(
        required_tool_obligation_ledger,
        selected_workflow_trace_payload,
        completion_report_payload,
    )
    tool_call_validation_failure_context = (
        _extract_tool_call_validation_failure_context(aux_llm_calls)
    )
    execution_summary = _summarise_tool_execution_context(
        workflow_routing=workflow_routing,
        turn_execution_diagnostics=(
            turn_execution_diagnostics
            if isinstance(turn_execution_diagnostics, Mapping)
            else None
        ),
        aux_llm_calls=aux_llm_calls,
        serialised_invocations=serialised_invocations,
        selected_workflow_trace=selected_workflow_trace_payload,
        workflow_failure_evidence=workflow_failure_evidence,
    )
    dispatched_workflow_id = _safe_str(execution_summary.get("dispatch_workflow_id"))
    if dispatched_workflow_id and not selected_workflow_id:
        selected_workflow_id = dispatched_workflow_id
        execution_summary = dict(execution_summary)
        if not _safe_str(execution_summary.get("selected_workflow_id")):
            execution_summary["selected_workflow_id"] = dispatched_workflow_id
            execution_summary["selected_workflow_id_source"] = "dispatch_workflow_id"
    (
        execution_surface_successful_tools,
        execution_surface_failed_tools,
        execution_surface_observed_tools,
        execution_surface_observations,
    ) = _augment_tool_outcomes_with_execution_surfaces(
        successful_tools=successful_tools,
        failed_tools=failed_tools,
        execution_summary=execution_summary,
    )
    actual_successful_tool_lookup = {
        _tool_requirement_key(tool_name)
        for tool_name in successful_tools
        if isinstance(tool_name, str) and tool_name.strip()
    }
    actual_failed_tool_lookup = {
        _tool_requirement_key(tool_name)
        for tool_name in failed_tools
        if isinstance(tool_name, str) and tool_name.strip()
    }
    execution_surface_successful_equivalent_tools = [
        tool_name
        for tool_name in execution_surface_successful_tools
        if _tool_requirement_key(tool_name) not in actual_successful_tool_lookup
    ]
    execution_surface_failed_equivalent_tools = [
        tool_name
        for tool_name in execution_surface_failed_tools
        if _tool_requirement_key(tool_name) not in actual_failed_tool_lookup
    ]
    successful_equivalent_lookup = {
        _tool_requirement_key(tool_name)
        for tool_name in execution_surface_successful_equivalent_tools
    }
    failed_equivalent_lookup = {
        _tool_requirement_key(tool_name)
        for tool_name in execution_surface_failed_equivalent_tools
    }
    execution_surface_successful_equivalent_observations = [
        observation
        for observation in execution_surface_observations
        if _tool_requirement_key(observation.get("tool"))
        in successful_equivalent_lookup
        and (_safe_str(observation.get("status")) or "").lower() == "success"
    ]
    execution_surface_failed_equivalent_observations = [
        observation
        for observation in execution_surface_observations
        if _tool_requirement_key(observation.get("tool")) in failed_equivalent_lookup
        and (_safe_str(observation.get("status")) or "").lower()
        in {"failed", "failure", "error"}
    ]
    successful_observation_lookup = {
        _tool_requirement_key(observation.get("tool"))
        for observation in execution_surface_successful_equivalent_observations
    }
    failed_observation_lookup = {
        _tool_requirement_key(observation.get("tool"))
        for observation in execution_surface_failed_equivalent_observations
    }
    execution_surface_successful_equivalent_tool_names = [
        tool_name
        for tool_name in execution_surface_successful_equivalent_tools
        if _tool_requirement_key(tool_name) not in successful_observation_lookup
    ]
    execution_surface_failed_equivalent_tool_names = [
        tool_name
        for tool_name in execution_surface_failed_equivalent_tools
        if _tool_requirement_key(tool_name) not in failed_observation_lookup
    ]
    if execution_surface_observed_tools:
        execution_summary = dict(execution_summary)
        execution_summary["execution_surface_observed_tool_names"] = list(
            execution_surface_observed_tools
        )
        execution_summary["execution_surface_successful_tool_names"] = [
            tool_name
            for tool_name in execution_surface_observed_tools
            if _tool_requirement_key(tool_name) in successful_equivalent_lookup
        ]
        execution_summary["execution_surface_failed_tool_names"] = [
            tool_name
            for tool_name in execution_surface_observed_tools
            if _tool_requirement_key(tool_name) in failed_equivalent_lookup
        ]
        execution_summary["execution_surface_observations"] = [
            dict(observation) for observation in execution_surface_observations
        ]
    if activated_conditional_required_tools:
        execution_summary = dict(execution_summary)
        execution_summary["activated_conditional_required_tools"] = list(
            activated_conditional_required_tools
        )
    if isinstance(obligation_carry_forward_projection, Mapping) and (
        obligation_carry_forward_projection.get("suppressed_prior_obligations")
        or obligation_carry_forward_projection.get("carried_forward_obligations")
        or obligation_carry_forward_projection.get("suppression_reason")
    ):
        execution_summary = dict(execution_summary)
        execution_summary["expected_outcome_obligation_carry_forward"] = dict(
            obligation_carry_forward_projection
        )
    required_tool_obligation_ledger_payload = build_required_tool_obligation_ledger(
        required_tools_by_source=required_tool_sources,
        # The bounded ledger never persists raw invocation payloads, but it must
        # validate targets against the actual arguments.  The compact TER
        # projection intentionally omits those arguments and is therefore not a
        # valid input to target-closure accounting.
        invocations=tool_invocations,
        observed_equivalent_successful_tools=(
            execution_surface_successful_equivalent_tool_names
        ),
        observed_equivalent_failed_tools=execution_surface_failed_equivalent_tool_names,
        observed_equivalent_successful_executions=(
            execution_surface_successful_equivalent_observations
        ),
        observed_equivalent_failed_executions=(
            execution_surface_failed_equivalent_observations
        ),
        tool_call_validation_failure_context=tool_call_validation_failure_context,
        target_contract_state=target_contract_state_from_context(
            workflow_required_effect_context_surfaces
        ),
        existing_ledger=existing_required_tool_obligation_ledger,
        method_catalogue=method_catalogue,
    )
    effective_successful_write_tools = _dedupe_string_sequence(
        [
            *successful_write_tools,
            *(
                tool_name
                for tool_name in execution_surface_successful_tools
                if _is_write_tool(tool_name)
            ),
        ]
    )
    target_bound_missing_prompt_tools = _target_bound_missing_prompt_tools(
        required_tools=effective_required_prompt_tools,
        tool_invocations=tool_invocations,
    )
    target_bound_missing_lookup = {
        _tool_requirement_key(tool_name)
        for tool_name in target_bound_missing_prompt_tools
        if isinstance(tool_name, str) and tool_name.strip()
    }
    successful_tool_lookup = {
        _tool_requirement_key(tool_name)
        for tool_name in execution_surface_successful_tools
        if isinstance(tool_name, str) and tool_name.strip()
    }
    effective_missing_prompt_tools = [
        tool_name
        for tool_name in effective_required_prompt_tools
        if _tool_requirement_key(tool_name) not in successful_tool_lookup
        or _tool_requirement_key(tool_name) in target_bound_missing_lookup
    ]
    prompt_required_successful_tools = [
        tool_name
        for tool_name in execution_surface_successful_tools
        if _tool_requirement_key(tool_name) not in target_bound_missing_lookup
    ]
    representation_effects_contract = _build_representation_required_effects_contract(
        aux_llm_calls=aux_llm_calls,
    )
    workflow_required_effects_contract, workflow_required_effects_contract_source = (
        _resolve_workflow_required_effects_contract(
            workflow_id=selected_workflow_id,
            selected_workflow_trace=selected_workflow_trace_payload,
            completion_report=completion_report,
        )
    )
    prompt_required_mutation_contract = (
        None
        if isinstance(representation_effects_contract, Mapping)
        else _build_prompt_required_mutation_contract(
            prompt_text=prompt_text,
            aux_llm_calls=aux_llm_calls,
            required_prompt_tools=effective_required_prompt_tools,
        )
    )
    # A selected workflow's represented required-effects contract is the
    # completion authority for that execution. Turn-level required tool names
    # remain routing/planning guidance and telemetry, but must not be promoted
    # into a second, potentially contradictory hard-effects contract. Generic
    # tool execution still uses the prompt contract when no workflow-specific
    # effects authority exists.
    prompt_required_evidence_contract = (
        None
        if isinstance(workflow_required_effects_contract, Mapping)
        else _build_prompt_required_evidence_contract(
            prompt_text=prompt_text,
            aux_llm_calls=aux_llm_calls,
            required_prompt_tools=effective_required_prompt_tools,
            target_concept_ids=required_evidence_target_concept_ids,
        )
    )
    prompt_required_evidence_contract = _apply_target_concepts_to_evidence_contract(
        contract=prompt_required_evidence_contract,
        target_concept_ids=required_evidence_target_concept_ids,
    )
    workflow_required_effects_contract = _apply_target_concepts_to_evidence_contract(
        contract=workflow_required_effects_contract,
        target_concept_ids=required_evidence_target_concept_ids,
    )
    representation_effects = _materialise_required_effects_from_contract(
        contract=representation_effects_contract,
        successful_tools=successful_tools,
        failed_tools=failed_tools,
        blocked_tools=blocked_tools,
        tool_invocations=tool_invocations,
    )
    prompt_required_mutation_effects = _materialise_required_effects_from_contract(
        contract=prompt_required_mutation_contract,
        successful_tools=prompt_required_successful_tools,
        failed_tools=execution_surface_failed_tools,
        blocked_tools=blocked_tools,
        tool_invocations=tool_invocations,
    )
    prompt_required_evidence_effects = _materialise_required_effects_from_contract(
        contract=prompt_required_evidence_contract,
        successful_tools=prompt_required_successful_tools,
        failed_tools=execution_surface_failed_tools,
        blocked_tools=blocked_tools,
        tool_invocations=tool_invocations,
    )
    workflow_required_effects = _materialise_required_effects_from_contract(
        contract=workflow_required_effects_contract,
        successful_tools=execution_surface_successful_tools,
        failed_tools=execution_surface_failed_tools,
        blocked_tools=blocked_tools,
        tool_invocations=tool_invocations,
        context_surfaces=workflow_required_effect_context_surfaces,
    )

    required_effects: list[dict[str, Any]] = []
    required_effects.extend(representation_effects)
    required_effects.extend(prompt_required_mutation_effects)
    required_effects.extend(prompt_required_evidence_effects)
    required_effects.extend(workflow_required_effects)

    required_effects = _apply_tool_call_validation_failures_to_required_effects(
        required_effects=required_effects,
        validation_failure_context=tool_call_validation_failure_context,
    )

    mutation_effects: list[dict[str, Any]] = []
    if not representation_effects and not workflow_required_effects:
        # Mutation effects must remain tool-authored or workflow-authored.
        # Do not fall back to generic observed-tool mutation contracts.
        mutation_effects = _build_tool_authored_mutation_effects(
            tool_invocations=tool_invocations,
        )
    required_effects.extend(mutation_effects)

    required_tool_effect = required_tool_obligation_effect(
        required_tool_obligation_ledger_payload
    )
    required_tool_blocking_codes = {
        code
        for code in _dedupe_string_sequence(
            required_tool_obligation_ledger_payload.get("blocking_failure_codes") or []
        )
    }
    represented_required_tool_keys = {
        _tool_requirement_key(tool_name)
        for effect in required_effects
        if isinstance(effect, Mapping)
        for tool_name in _dedupe_string_sequence(effect.get("required_tools") or [])
    }
    unrepresented_unsatisfied_required_tools = [
        tool_name
        for tool_name in _dedupe_string_sequence(
            required_tool_obligation_ledger_payload.get("unsatisfied_required_tools")
            or []
        )
        if _tool_requirement_key(tool_name) not in represented_required_tool_keys
    ]
    if (
        not isinstance(workflow_required_effects_contract, Mapping)
        and isinstance(required_tool_effect, Mapping)
        and (
            not required_effects
            or bool(
                required_tool_blocking_codes - {BLOCKER_REQUIRED_TOOL_NOT_PLANNED}
            )
            or bool(unrepresented_unsatisfied_required_tools)
        )
    ):
        required_effects.append(dict(required_tool_effect))

    if (
        not required_effects
        and not mutation_effects
        and not successful_write_tools
        and not representation_effects
        and not workflow_required_effects
    ):
        tool_execution_effect = _infer_tool_execution_required_effect(
            execution_summary=execution_summary
        )
        if tool_execution_effect is not None:
            required_effects.append(tool_execution_effect)
        custom_workflow_effect = _infer_custom_workflow_required_effect(
            execution_summary=execution_summary
        )
        if custom_workflow_effect is not None:
            required_effects.append(custom_workflow_effect)

    execution_summary = _apply_required_effect_tool_obligations_to_execution_summary(
        execution_summary=execution_summary,
        required_effects=required_effects,
        serialised_invocations=serialised_invocations,
        observed_equivalent_tools=execution_surface_observed_tools,
    )
    if int(required_tool_obligation_ledger_payload.get("required_tool_count") or 0) > 0:
        execution_summary = dict(execution_summary)
        execution_summary["required_tool_obligations"] = dict(
            required_tool_obligation_ledger_payload
        )
        execution_summary["required_tool_obligation_unsatisfied_count"] = int(
            required_tool_obligation_ledger_payload.get("unsatisfied_count") or 0
        )
        execution_summary["required_tool_obligation_blocking_failure_codes"] = (
            _dedupe_string_sequence(
                required_tool_obligation_ledger_payload.get("blocking_failure_codes")
                or []
            )
        )
        summary_failure_codes = _dedupe_string_sequence(
            [
                *(execution_summary.get("failure_codes") or []),
                *execution_summary["required_tool_obligation_blocking_failure_codes"],
            ]
        )
        execution_summary["failure_codes"] = summary_failure_codes
        tool_execution_raw = execution_summary.get("tool_execution")
        tool_execution = (
            dict(tool_execution_raw) if isinstance(tool_execution_raw, Mapping) else {}
        )
        tool_execution["required_tool_obligations"] = dict(
            required_tool_obligation_ledger_payload
        )
        tool_execution["required_tool_obligation_unsatisfied_count"] = (
            execution_summary["required_tool_obligation_unsatisfied_count"]
        )
        tool_execution["failure_codes"] = summary_failure_codes
        execution_summary["tool_execution"] = tool_execution
    failed_validation_tools = _dedupe_string_sequence(
        tool_call_validation_failure_context.get("failed_tools") or []
    )
    if failed_validation_tools:
        execution_summary = dict(execution_summary)
        execution_summary["tool_call_validation_failed_tools"] = list(
            failed_validation_tools
        )
        execution_summary["tool_call_validation_error_count"] = sum(
            len(failure.get("errors") or [])
            for failure in (
                tool_call_validation_failure_context.get("failures_by_tool") or {}
            ).values()
            if isinstance(failure, Mapping)
        )
        repair_outcome = _safe_str(
            tool_call_validation_failure_context.get("repair_outcome")
        )
        repair_stop_reason = _safe_str(
            tool_call_validation_failure_context.get("repair_stop_reason")
        )
        if repair_outcome:
            execution_summary["tool_call_repair_outcome"] = repair_outcome
        if repair_stop_reason:
            execution_summary["tool_call_repair_stop_reason"] = repair_stop_reason
    postcondition_checks = _build_postcondition_checks(
        required_effects=required_effects,
        successful_write_tools=effective_successful_write_tools,
        successful_verification_tools=successful_verification_tools,
    )
    critic_summary = _summarise_check_counts(postcondition_checks)

    completion_claim = _extract_completion_claim_signals(
        response_text=response_text,
        aux_llm_calls=aux_llm_calls,
    )

    completion_gate = _derive_completion_gate(
        required_effects=required_effects,
        postcondition_checks=postcondition_checks,
        completion_claim_validated=bool(completion_claim["validated"]),
        execution_summary=execution_summary,
    )
    (
        prompt_required_evidence_answer_consistency_blocker,
        prompt_required_evidence_answer_consistency_source,
    ) = _resolve_prompt_required_evidence_answer_consistency_blocker(
        critic_verdict=critic_verdict,
    )
    existing_gate_failure_codes = {
        code.lower()
        for code in _dedupe_string_sequence(
            completion_gate.get("blocking_failure_codes") or []
        )
        if isinstance(code, str) and code.strip()
    }
    if isinstance(prompt_required_evidence_answer_consistency_blocker, Mapping) and (
        bool(completion_gate.get("safe_to_claim_completion", False))
        or existing_gate_failure_codes.issubset({"postcondition_inconclusive"})
    ):
        completion_gate = _apply_completion_gate_blocker(
            completion_gate=completion_gate,
            blocker=prompt_required_evidence_answer_consistency_blocker,
        )

    authoritative_receipt_required = bool(
        isinstance(critic_verdict, Mapping)
        and "terminal_outcome_receipt" in critic_verdict
    )
    terminal_outcome_receipt_candidate = (
        critic_verdict.get("terminal_outcome_receipt")
        if isinstance(critic_verdict, Mapping)
        else None
    )
    (
        terminal_outcome_receipt,
        terminal_outcome_receipt_validation,
    ) = validate_terminal_outcome_receipt(
        terminal_outcome_receipt_candidate,
        completion_gate=completion_gate,
        required=authoritative_receipt_required,
    )
    completion_gate = apply_terminal_outcome_receipt_to_completion_gate(
        completion_gate,
        receipt=terminal_outcome_receipt,
        validation=terminal_outcome_receipt_validation,
        authoritative_receipt_required=authoritative_receipt_required,
    )
    tool_observation_ledger_payload = build_tool_observation_ledger(
        existing_ledger=tool_observation_ledger_payload,
        terminal_outcome_receipt=terminal_outcome_receipt,
        terminal_outcome_receipt_validation=terminal_outcome_receipt_validation,
    )

    requested_evidence_lineage = _build_requested_evidence_lineage(
        response_text=response_text,
        final_answer_synthesis=final_answer_synthesis,
        required_effects=required_effects,
        turn_expected_outcome_contract=(
            resolved_turn_expected_outcome_contract.to_state_payload()
            if turn_expected_outcome_contract_available
            else None
        ),
        prompt_required_evidence_contract=prompt_required_evidence_contract,
        workflow_required_effects_contract=workflow_required_effects_contract,
        workflow_required_effects_contract_source=workflow_required_effects_contract_source,
        completion_gate=completion_gate,
    )
    if isinstance(requested_evidence_lineage, Mapping):
        completion_gate = dict(completion_gate)
        gate_evidence_payload = (
            dict(completion_gate.get("evidence_payload"))
            if isinstance(completion_gate.get("evidence_payload"), Mapping)
            else {}
        )
        gate_evidence_payload["requested_evidence_lineage"] = dict(
            requested_evidence_lineage
        )
        completion_gate["evidence_payload"] = gate_evidence_payload

    latest_progress = (
        turn_execution_diagnostics.get("latest_progress")
        if isinstance(turn_execution_diagnostics, Mapping)
        else None
    )
    diagnostic_events = []
    retry = {"attempts": 0, "budget": 0, "reason": None}
    if isinstance(latest_progress, Mapping):
        raw_events = latest_progress.get("diagnostic_events")
        if isinstance(raw_events, list):
            diagnostic_events = [
                item for item in raw_events if isinstance(item, Mapping)
            ]
        try:
            retry["attempts"] = int(latest_progress.get("retry_attempts") or 0)
        except Exception:
            retry["attempts"] = 0
        try:
            retry["budget"] = int(latest_progress.get("retry_budget") or 0)
        except Exception:
            retry["budget"] = 0
        retry["reason"] = _safe_str(latest_progress.get("retry_reason"))

    runtime_stages: list[str] = []
    for event in diagnostic_events:
        if not isinstance(event, Mapping):
            continue
        phase = _safe_str(event.get("phase"))
        stage = _safe_str(event.get("stage"))
        if phase:
            runtime_stages.append(phase)
        if stage:
            runtime_stages.append(stage)

    phase_history = (
        turn_execution_diagnostics.get("phase_history")
        if isinstance(turn_execution_diagnostics, Mapping)
        else None
    )
    if isinstance(phase_history, list):
        for entry in phase_history:
            if not isinstance(entry, Mapping):
                continue
            phase = _safe_str(entry.get("phase"))
            if phase:
                runtime_stages.append(phase)

    workflow_stage_path = (
        turn_execution_diagnostics.get("workflow_stage_path")
        if isinstance(turn_execution_diagnostics, Mapping)
        else None
    )
    if not isinstance(workflow_stage_path, Mapping):
        workflow_stage_path = build_conversation_turn_stage_path(
            runtime_stages=runtime_stages,
            workflow_id=selected_workflow_id,
            selected_workflow_id=selected_workflow_id,
        )

    execution_summary_with_contract = dict(execution_summary)
    if int(tool_observation_ledger_payload.get("observation_count") or 0) > 0:
        execution_summary_with_contract["tool_observation_count"] = int(
            tool_observation_ledger_payload.get("observation_count") or 0
        )
        execution_summary_with_contract["tool_observation_status_counts"] = dict(
            tool_observation_ledger_payload.get("status_counts") or {}
        )
        execution_summary_with_contract["tool_observation_observed_tools"] = list(
            tool_observation_ledger_payload.get("observed_tools") or []
        )
    primary_required_effects_contract = (
        representation_effects_contract
        if isinstance(representation_effects_contract, Mapping)
        else (
            prompt_required_mutation_contract
            if isinstance(prompt_required_mutation_contract, Mapping)
            else None
        )
    )
    if primary_required_effects_contract is None and isinstance(
        prompt_required_evidence_contract, Mapping
    ):
        primary_required_effects_contract = prompt_required_evidence_contract
    if isinstance(primary_required_effects_contract, Mapping):
        execution_summary_with_contract["required_effects_contract_id"] = _safe_str(
            primary_required_effects_contract.get("contract_id")
        )
        execution_summary_with_contract["required_effects_contract_domain"] = _safe_str(
            primary_required_effects_contract.get("domain_profile_id")
        )
        execution_summary_with_contract[
            "required_effects_contract_domain_concept_id"
        ] = _safe_str(
            primary_required_effects_contract.get("domain_profile_concept_id")
        )
        execution_summary_with_contract["required_effects_contract_intent"] = _safe_str(
            primary_required_effects_contract.get("intent_class")
        )
        execution_summary_with_contract["required_effects_contract_profile_source"] = (
            _safe_str(primary_required_effects_contract.get("profile_source"))
        )
        execution_summary_with_contract[
            "required_effects_contract_profile_version_hash"
        ] = _safe_str(primary_required_effects_contract.get("profile_version_hash"))
        execution_summary_with_contract["required_effects_declared_count"] = len(
            primary_required_effects_contract.get("required_effects") or []
        )
        profile_resolution = primary_required_effects_contract.get("profile_resolution")
        if isinstance(profile_resolution, Mapping):
            execution_summary_with_contract[
                "required_effects_contract_profile_requested_ids"
            ] = _dedupe_string_sequence(
                profile_resolution.get("requested_profile_concept_ids") or []
            )
            execution_summary_with_contract[
                "required_effects_contract_profile_loaded_ids"
            ] = _dedupe_string_sequence(
                profile_resolution.get("loaded_profile_concept_ids") or []
            )
            execution_summary_with_contract[
                "required_effects_contract_profile_selected_id"
            ] = _safe_str(profile_resolution.get("selected_profile_concept_id"))
            execution_summary_with_contract["required_effects_contract_fail_closed"] = (
                bool(profile_resolution.get("fail_closed"))
            )
            execution_summary_with_contract[
                "required_effects_contract_fail_closed_reason"
            ] = _safe_str(profile_resolution.get("fail_closed_reason"))
    execution_summary_with_contract["search_evidence_count"] = len(
        search_evidence_payload
    )
    execution_summary_with_contract["turn_expected_outcome_contract_field_count"] = len(
        turn_expected_outcome_contract_payload
    )
    execution_summary_with_contract["turn_expected_outcome_target_concept_ids"] = list(
        resolved_turn_expected_outcome_contract.target_concept_ids
    )
    execution_summary_with_contract["required_evidence_target_concept_ids"] = list(
        required_evidence_target_concept_ids
    )
    execution_summary_with_contract["turn_expected_outcome_contract_sources"] = list(
        resolved_turn_expected_outcome_contract.sources
    )
    execution_summary_with_contract["required_evidence_answer_consistency_blocked"] = (
        bool(prompt_required_evidence_answer_consistency_blocker)
    )
    if isinstance(workflow_required_effects_contract, Mapping):
        execution_summary_with_contract["workflow_required_effects_contract_id"] = (
            _safe_str(workflow_required_effects_contract.get("contract_id"))
        )
        execution_summary_with_contract["workflow_required_effects_declared_count"] = (
            len(workflow_required_effects_contract.get("required_effects") or [])
        )
        execution_summary_with_contract["workflow_required_effects_contract_source"] = (
            workflow_required_effects_contract_source
        )
    execution_summary_with_contract["workflow_required_effects_materialised_count"] = (
        len(workflow_required_effects)
    )
    execution_summary_with_contract["required_evidence_answer_consistency_source"] = (
        prompt_required_evidence_answer_consistency_source
    )
    if isinstance(requested_evidence_lineage, Mapping):
        execution_summary_with_contract["requested_evidence_lineage_observed"] = True
        execution_summary_with_contract["requested_evidence_field_count"] = len(
            requested_evidence_lineage.get("requested_fields") or []
        )
        execution_summary_with_contract["requested_evidence_unresolved_field_count"] = (
            len(requested_evidence_lineage.get("unresolved_requested_fields") or [])
        )
        execution_summary_with_contract["requested_evidence_resolver_chain_count"] = (
            len(requested_evidence_lineage.get("resolver_chains") or [])
        )
        execution_summary_with_contract[
            "requested_evidence_unresolved_resolver_chain_count"
        ] = len(requested_evidence_lineage.get("unresolved_resolver_chains") or [])
    context_adjudication_projection = build_turn_context_adjudication_projection(
        (
            (
                "turn_execution_diagnostics",
                (
                    turn_execution_diagnostics
                    if isinstance(turn_execution_diagnostics, Mapping)
                    else None
                ),
            ),
            (
                "selected_workflow_trace",
                (
                    selected_workflow_trace_payload
                    if isinstance(selected_workflow_trace_payload, Mapping)
                    else None
                ),
            ),
            (
                "completion_report",
                (
                    completion_report_payload
                    if isinstance(completion_report_payload, Mapping)
                    else None
                ),
            ),
        )
    )
    if isinstance(context_adjudication_projection, Mapping):
        execution_summary_with_contract["context_adjudication_observed"] = True
        context_adjudication_mode = _safe_str(
            context_adjudication_projection.get("mode")
        )
        if context_adjudication_mode:
            execution_summary_with_contract["context_adjudication_mode"] = (
                context_adjudication_mode
            )
        context_adjudication_source = _safe_str(
            context_adjudication_projection.get("source")
        )
        if context_adjudication_source:
            execution_summary_with_contract["context_adjudication_source"] = (
                context_adjudication_source
            )
    if isinstance(obligation_carry_forward_projection, Mapping):
        execution_summary_with_contract["expected_outcome_obligation_carry_forward"] = (
            dict(obligation_carry_forward_projection)
        )
        suppressed_prior_obligations = list(
            obligation_carry_forward_projection.get("suppressed_prior_obligations")
            or ()
        )
        if suppressed_prior_obligations:
            execution_summary_with_contract["suppressed_prior_obligation_tools"] = (
                suppressed_prior_obligations
            )
            execution_summary_with_contract["suppressed_prior_obligation_reason"] = (
                _safe_str(obligation_carry_forward_projection.get("suppression_reason"))
            )
    execution_summary_with_contract["final_answer_synthesis_observed"] = bool(
        final_answer_synthesis
    )
    if isinstance(final_answer_synthesis, Mapping):
        tool_projection = final_answer_synthesis.get("tool_evidence_projection")
        if isinstance(tool_projection, Mapping):
            execution_summary_with_contract[
                "final_answer_synthesis_projection_count"
            ] = _safe_non_negative_int(tool_projection.get("projection_count"))
            execution_summary_with_contract[
                "final_answer_synthesis_projection_tools"
            ] = _dedupe_string_sequence(tool_projection.get("tools") or [])
    if effective_required_prompt_tools:
        execution_summary_with_contract["required_prompt_tools"] = list(
            effective_required_prompt_tools
        )
        execution_summary_with_contract["missing_prompt_tools"] = list(
            effective_missing_prompt_tools
        )

    workflow_routing_diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery=workflow_discovery,
        workflow_routing=workflow_routing,
        turn_execution_diagnostics=(
            turn_execution_diagnostics
            if isinstance(turn_execution_diagnostics, Mapping)
            else None
        ),
        aux_llm_calls=aux_llm_calls,
        turn_expected_outcome_contract=(
            resolved_turn_expected_outcome_contract.to_state_payload()
            if turn_expected_outcome_contract_available
            else None
        ),
        execution_summary=execution_summary_with_contract,
    )

    llm_call_log = [
        {str(key): value for key, value in entry.items() if isinstance(key, str)}
        for entry in (llm_calls or ())
        if isinstance(entry, Mapping)
    ]
    aux_llm_call_log = [
        {str(key): value for key, value in entry.items() if isinstance(key, str)}
        for entry in (aux_llm_calls or ())
        if isinstance(entry, Mapping)
    ]

    decision_attribution_diagnostics = {
        "workflow_discovery": workflow_routing_diagnostics.get("discovery"),
        "workflow_routing_diagnostics": workflow_routing_diagnostics,
        "workflow_selection": {
            "selected_workflow_id": selected_workflow_id,
            "selector_source": selector_source,
            "selector_verdict": selector_verdict,
        },
        "selected_workflow_trace": selected_workflow_trace_payload,
        "workflow_model_policy": (
            selected_workflow_trace_payload.get("workflow_model_policy")
            if isinstance(selected_workflow_trace_payload, Mapping)
            else None
        ),
        "completion_gate": completion_gate,
        "completion_gate_verdict": completion_gate_verdict,
    }
    decision_attribution = build_turn_decision_attribution(
        diagnostics=decision_attribution_diagnostics,
        aux_entries=aux_llm_call_log,
    )

    record_payload = {
        "schema_version": TURN_EXECUTION_RECORD_SCHEMA_VERSION,
        "request_id": _safe_str(request_id),
        "session_id": _safe_str(session_id),
        "namespace": _safe_str(namespace),
        "actor_concept_id": resolved_actor_concept_id,
        "actor_identity_source": actor_identity_source,
        "user_id": _safe_str(user_id),
        "org_id": _safe_str(org_id),
        "created_at_utc": _normalise_iso_timestamp(interaction_timestamp_utc),
        "prompt": {
            "preview": (
                prompt_text[:1000]
                if isinstance(prompt_text, str)
                else _safe_str(prompt_text)
            ),
            "sha256": _hash_text(prompt_text),
            "source": "user_message",
        },
        "workflow_selection": {
            "selected_workflow_id": selected_workflow_id,
            "selector_verdict": selector_verdict,
            "selector_source": selector_source,
            "workflow_discovery": workflow_discovery_normalised,
        },
        "turn_expected_outcome_contract": (
            dict(turn_expected_outcome_contract_payload)
            if turn_expected_outcome_contract_available
            else None
        ),
        "turn_expected_outcome_contract_state": (
            resolved_turn_expected_outcome_contract.to_state_payload()
            if turn_expected_outcome_contract_available
            else None
        ),
        "workflow_routing_diagnostics": workflow_routing_diagnostics,
        "llm_calls": llm_call_log,
        "aux_llm_calls": aux_llm_call_log,
        "required_effects": required_effects,
        "execution": {
            "tool_invocations": serialised_invocations,
            "search_evidence": search_evidence_payload,
            "tool_observation_ledger": (
                dict(tool_observation_ledger_payload)
                if int(tool_observation_ledger_payload.get("observation_count") or 0)
                > 0
                or isinstance(
                    tool_observation_ledger_payload.get(
                        "terminal_outcome_receipt_projection"
                    ),
                    Mapping,
                )
                else None
            ),
            "llm_calls": llm_call_log,
            "aux_llm_calls": aux_llm_call_log,
            "summary": execution_summary_with_contract,
            "turn_expected_outcome_contract": (
                dict(turn_expected_outcome_contract_payload)
                if turn_expected_outcome_contract_available
                else None
            ),
            "turn_expected_outcome_contract_state": (
                resolved_turn_expected_outcome_contract.to_state_payload()
                if turn_expected_outcome_contract_available
                else None
            ),
            "required_effects_contract": primary_required_effects_contract,
            "prompt_required_mutation_contract": prompt_required_mutation_contract,
            "workflow_required_effects_contract": workflow_required_effects_contract,
            "requested_evidence_lineage": (
                dict(requested_evidence_lineage)
                if isinstance(requested_evidence_lineage, Mapping)
                else None
            ),
            "context_adjudication": (
                dict(context_adjudication_projection)
                if isinstance(context_adjudication_projection, Mapping)
                else None
            ),
            "required_prompt_tools": (
                list(effective_required_prompt_tools)
                if effective_required_prompt_tools
                else None
            ),
            "missing_prompt_tools": (
                list(effective_missing_prompt_tools)
                if effective_required_prompt_tools
                else None
            ),
            "required_tool_obligations": (
                dict(required_tool_obligation_ledger_payload)
                if int(
                    required_tool_obligation_ledger_payload.get("required_tool_count")
                    or 0
                )
                > 0
                else None
            ),
            "diagnostic_events": diagnostic_events,
            "retry": retry,
            "workflow_stage_model": build_conversation_turn_stage_model_snapshot(),
            "workflow_stage_path": workflow_stage_path,
            "selected_workflow_trace": selected_workflow_trace_payload,
        },
        "context_adjudication": (
            dict(context_adjudication_projection)
            if isinstance(context_adjudication_projection, Mapping)
            else None
        ),
        "decision_attribution": decision_attribution,
        "postcondition_checks": postcondition_checks,
        "critic": {
            "enabled": True,
            "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
            "summary": critic_summary,
            "verdict": critic_verdict,
        },
        "terminal_outcome_receipt": terminal_outcome_receipt,
        "terminal_outcome_receipt_validation": terminal_outcome_receipt_validation,
        "completion_gate": completion_gate,
        "completion_gate_verdict": completion_gate_verdict,
        "completion_report": completion_report,
        "final_answer_synthesis": final_answer_synthesis,
        "requested_evidence_lineage": requested_evidence_lineage,
        "final_response": {
            "response_sha256": _hash_text(response_text),
            "completion_claim_detected": bool(completion_claim["detected"]),
            "completion_claim_validated": bool(completion_claim["validated"]),
            "synthesis_observed": bool(final_answer_synthesis),
            "requested_evidence_lineage_observed": bool(requested_evidence_lineage),
        },
    }
    return ensure_turn_execution_record_execution_correctness(record_payload)


def _ensure_turn_execution_indexes(collection) -> None:
    global _TURN_EXECUTION_INDEXES_READY
    if _TURN_EXECUTION_INDEXES_READY:
        return

    with _TURN_EXECUTION_INDEXES_LOCK:
        if _TURN_EXECUTION_INDEXES_READY:
            return
        try:
            existing_indexes = [idx.get("name") for idx in collection.list_indexes()]
            if "request_id_unique" not in existing_indexes:
                collection.create_index(
                    [("request_id", ASCENDING)],
                    unique=True,
                    name="request_id_unique",
                )
            if "namespace_created_desc" not in existing_indexes:
                collection.create_index(
                    [("namespace", ASCENDING), ("created_at_utc", DESCENDING)],
                    name="namespace_created_desc",
                )
            if "session_created_desc" not in existing_indexes:
                collection.create_index(
                    [("session_id", ASCENDING), ("created_at_utc", DESCENDING)],
                    name="session_created_desc",
                )
            if "decision_created_desc" not in existing_indexes:
                collection.create_index(
                    [
                        ("completion_gate.decision", ASCENDING),
                        ("created_at_utc", DESCENDING),
                    ],
                    name="decision_created_desc",
                )
            if "effect_status_created_desc" not in existing_indexes:
                collection.create_index(
                    [
                        ("required_effects.status", ASCENDING),
                        ("created_at_utc", DESCENDING),
                    ],
                    name="effect_status_created_desc",
                )
            if "workflow_created_desc" not in existing_indexes:
                collection.create_index(
                    [
                        ("workflow_selection.selected_workflow_id", ASCENDING),
                        ("created_at_utc", DESCENDING),
                    ],
                    name="workflow_created_desc",
                )
        except OperationFailure as exc:
            logger.warning(
                "Index creation partially failed for turn_execution_records: %s",
                exc,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Could not ensure turn_execution_records indexes: %s",
                exc,
            )
        _TURN_EXECUTION_INDEXES_READY = True


def get_turn_execution_records_collection():
    db = get_db()
    if db is None:
        return None
    coll = db[TURN_EXECUTION_RECORDS_COLLECTION]
    _ensure_turn_execution_indexes(coll)
    return coll


def _turn_execution_mongo_comment(operation: str, detail: str | None = None):
    return build_mongo_operation_comment(
        service="turn_execution_record_service",
        collection=TURN_EXECUTION_RECORDS_COLLECTION,
        operation=operation,
        detail=detail,
    )


def _turn_execution_find_one(
    collection,
    query: Mapping[str, Any],
    *,
    projection: Mapping[str, Any] | None = None,
    sort: Any = None,
    operation: str,
    detail: str | None = None,
):
    kwargs: dict[str, Any] = {}
    if projection is not None:
        kwargs["projection"] = projection
    if sort is not None:
        kwargs["sort"] = sort
    comment = _turn_execution_mongo_comment(operation, detail=detail)
    if comment is not None:
        kwargs["comment"] = comment
    started_at = time.perf_counter()
    success = False
    error_type: str | None = None
    try:
        result = collection.find_one(query, **kwargs)
        success = True
        return result
    except TypeError as exc:
        error_type = type(exc).__name__
        kwargs.pop("comment", None)
        try:
            result = collection.find_one(query, **kwargs)
            success = True
            return result
        except Exception as fallback_exc:
            error_type = type(fallback_exc).__name__
            raise
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        observe_mongo_operation(
            service="turn_execution_record_service",
            collection=TURN_EXECUTION_RECORDS_COLLECTION,
            operation=operation,
            started_at=started_at,
            success=success,
            detail=detail,
            error_type=error_type,
        )


def _turn_execution_update_one(
    collection,
    query: Mapping[str, Any],
    update: Mapping[str, Any],
    *,
    upsert: bool = False,
    operation: str,
    detail: str | None = None,
):
    kwargs: dict[str, Any] = {"upsert": upsert}
    comment = _turn_execution_mongo_comment(operation, detail=detail)
    if comment is not None:
        kwargs["comment"] = comment
    started_at = time.perf_counter()
    success = False
    error_type: str | None = None
    try:
        result = collection.update_one(query, update, **kwargs)
        success = True
        return result
    except TypeError as exc:
        error_type = type(exc).__name__
        kwargs.pop("comment", None)
        try:
            result = collection.update_one(query, update, **kwargs)
            success = True
            return result
        except Exception as fallback_exc:
            error_type = type(fallback_exc).__name__
            raise
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        observe_mongo_operation(
            service="turn_execution_record_service",
            collection=TURN_EXECUTION_RECORDS_COLLECTION,
            operation=operation,
            started_at=started_at,
            success=success,
            detail=detail,
            error_type=error_type,
        )


def _load_represented_terminal_outcome_receipt_default() -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any],
]:
    """Load the critic workflow's represented fail-closed receipt default.

    This path is used only when a failed/interrupted turn cannot finish the
    represented critic.  It deliberately reads the default from live
    Vontology rather than embedding a Python-side semantic fallback.
    """

    authority = {
        "source": "vontology_workflow_validation_default",
        "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
        "available": False,
    }
    try:
        from ..workflows.vontology_loader import (
            load_workflow_definition_from_vontology,
        )

        definition = load_workflow_definition_from_vontology(
            "#V#kb_mutation_postcondition_critic_workflow"
        )
        if definition is None:
            authority["reason"] = "represented_critic_workflow_unavailable"
            return None, None, authority
        for state_id, state in definition.states.items():
            for action in state.actions:
                validation_policy = action.validation_policy
                if not isinstance(validation_policy, Mapping):
                    continue
                defaults = validation_policy.get("json_field_defaults")
                if not isinstance(defaults, Mapping):
                    continue
                candidate = defaults.get("terminal_outcome_receipt")
                if not isinstance(candidate, Mapping):
                    continue
                provenance = candidate.get("provenance")
                if not isinstance(provenance, Mapping) or (
                    _safe_str(provenance.get("decision_source"))
                    != "represented_workflow_default"
                ):
                    continue
                receipt, validation = validate_terminal_outcome_receipt(
                    candidate,
                    required=True,
                )
                authority.update(
                    {
                        "available": isinstance(receipt, Mapping),
                        "state_id": _safe_str(state_id),
                        "validation": dict(validation),
                    }
                )
                return (
                    dict(receipt) if isinstance(receipt, Mapping) else None,
                    dict(validation),
                    authority,
                )
        authority["reason"] = "represented_critic_receipt_default_missing"
    except Exception as exc:
        authority.update(
            {
                "reason": "represented_critic_receipt_default_load_failed",
                "error_class": type(exc).__name__,
            }
        )
    return None, None, authority


def persist_failed_turn_execution_record(
    *,
    request_id: Any,
    terminal_status: str,
    error_text: Any = None,
    error_class: Any = None,
    session_id: Any = None,
    namespace: Any = None,
    user_id: Any = None,
    org_id: Any = None,
    prompt_text: Any = None,
    llm_debug_info: Mapping[str, Any] | None = None,
    progress_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a turn execution record for a turn that did not complete.

    JVNAUTOSCI-2502: records were previously written only through
    assistant-message chat persistence, so failed or cancelled turns left no
    record at all — exactly the turns that most need diagnostics. This
    best-effort path builds a record from whatever in-flight debug state
    exists and stamps an explicit terminal-failure envelope.
    """

    clean_request_id = _safe_str(request_id)
    if not clean_request_id:
        return {"updated": False, "reason": "missing_request_id"}
    clean_terminal_status = (_safe_str(terminal_status) or "failed").lower()

    debug = llm_debug_info if isinstance(llm_debug_info, Mapping) else {}

    def _debug_mapping(key: str) -> Mapping[str, Any] | None:
        value = debug.get(key)
        return value if isinstance(value, Mapping) else None

    def _debug_list(key: str) -> list[Any] | None:
        value = debug.get(key)
        return list(value) if isinstance(value, list) else None

    critic_verdict = _debug_mapping("critic_verdict")
    receipt_authority: dict[str, Any] | None = None
    if not isinstance(critic_verdict, Mapping):
        existing_receipt = debug.get("terminal_outcome_receipt")
        if isinstance(existing_receipt, Mapping):
            critic_verdict = {"terminal_outcome_receipt": dict(existing_receipt)}
    if not isinstance(critic_verdict, Mapping):
        represented_default, _default_validation, receipt_authority = (
            _load_represented_terminal_outcome_receipt_default()
        )
        if isinstance(represented_default, Mapping):
            critic_verdict = {
                "terminal_outcome_receipt": dict(represented_default),
            }

    try:
        record = build_turn_execution_record(
            request_id=clean_request_id,
            session_id=session_id,
            namespace=namespace,
            user_id=user_id,
            org_id=org_id,
            prompt_text=prompt_text,
            response_text=None,
            interaction_timestamp_utc=debug.get("interaction_timestamp_utc"),
            workflow_discovery=_debug_mapping("workflow_discovery"),
            workflow_routing=_debug_mapping("workflow_routing"),
            tool_invocations=_debug_list("invocations"),
            turn_execution_diagnostics=_debug_mapping("turn_execution_diagnostics"),
            aux_llm_calls=_debug_list("aux_llm_calls"),
            llm_calls=_debug_list("llm_calls"),
            selected_workflow_trace=_debug_mapping("selected_workflow_trace"),
            turn_expected_outcome_contract=_debug_mapping(
                "turn_expected_outcome_contract"
            ),
            critic_verdict=critic_verdict,
            completion_gate_verdict=_debug_mapping("completion_gate_verdict"),
            completion_report=_debug_mapping("completion_report"),
            required_prompt_tools=_debug_list("required_prompt_tools"),
            tool_observation_ledger=_debug_mapping("tool_observation_ledger"),
        )
    except Exception as exc:
        logger.warning(
            "Failed to build terminal-failure turn record for request_id=%s: %s",
            clean_request_id,
            exc,
        )
        record = {
            "schema_version": TURN_EXECUTION_RECORD_SCHEMA_VERSION,
            "request_id": clean_request_id,
        }
    if not isinstance(record, dict):
        record = {
            "schema_version": TURN_EXECUTION_RECORD_SCHEMA_VERSION,
            "request_id": clean_request_id,
        }

    record["execution_terminal_status"] = clean_terminal_status
    if isinstance(receipt_authority, Mapping):
        record["terminal_outcome_receipt_authority"] = dict(receipt_authority)
    record["terminal_failure"] = {
        "schema_version": "turn_terminal_failure.v1",
        "terminal_status": clean_terminal_status,
        "error": _safe_str(error_text),
        "error_class": _safe_str(error_class),
        "progress_snapshot": (
            dict(progress_snapshot) if isinstance(progress_snapshot, Mapping) else None
        ),
        "recorded_at_utc": _iso_utc(_now_utc()),
    }

    outcome = upsert_turn_execution_record_projection(
        record=record,
        user_id=_safe_str(user_id),
        session_id=_safe_str(session_id),
        namespace=_safe_str(namespace),
        org_id=_safe_str(org_id),
    )
    if isinstance(outcome, dict) and outcome.get("updated"):
        logger.info(
            "Persisted terminal-failure turn record request_id=%s status=%s",
            clean_request_id,
            clean_terminal_status,
        )
    return outcome


def upsert_turn_execution_record_projection(
    *,
    record: Mapping[str, Any],
    user_id: str | None = None,
    session_id: str | None = None,
    namespace: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {"updated": False, "reason": "invalid_record"}

    request_id = _safe_str(record.get("request_id"))
    if not request_id:
        return {"updated": False, "reason": "missing_request_id"}

    coll = get_turn_execution_records_collection()
    if coll is None:
        return {"updated": False, "reason": "collection_unavailable"}

    now = _now_utc()
    payload = ensure_turn_execution_record_execution_correctness(record)
    # Late observations arrive independently after the ordinary projection was
    # assembled.  They are append-only and must not be replaced by a later
    # whole-turn snapshot containing no, or stale, late-observation state.
    payload.pop("late_effect_observations", None)
    payload["request_id"] = request_id
    if _safe_str(user_id):
        payload["user_id"] = _safe_str(user_id)
    if _safe_str(session_id):
        payload["session_id"] = _safe_str(session_id)
    if _safe_str(namespace):
        payload["namespace"] = _safe_str(namespace)
    if _safe_str(org_id):
        payload["org_id"] = _safe_str(org_id)
    payload.setdefault("schema_version", TURN_EXECUTION_RECORD_SCHEMA_VERSION)
    payload.setdefault("created_at_utc", _iso_utc(now))
    payload["updated_at_utc"] = _iso_utc(now)
    compacted_payload = compact_debug_payload_for_storage(
        payload,
        root_kind="turn_execution_record",
        namespace=_safe_str(namespace),
        request_id=request_id,
        fail_soft=True,
    )
    if isinstance(compacted_payload.payload, Mapping):
        payload = dict(compacted_payload.payload)
    payload["request_id"] = request_id
    for field_name, raw_value in (
        ("namespace", namespace),
        ("user_id", user_id),
        ("org_id", org_id),
    ):
        clean_value = _safe_str(raw_value)
        if clean_value:
            payload[field_name] = clean_value
    payload.setdefault("schema_version", TURN_EXECUTION_RECORD_SCHEMA_VERSION)
    payload["updated_at_utc"] = _iso_utc(now)

    scope_query: dict[str, Any] = {"request_id": request_id}
    for field_name, raw_value in (
        ("namespace", namespace),
        ("user_id", user_id),
        ("org_id", org_id),
    ):
        clean_value = _safe_str(raw_value)
        if clean_value:
            scope_query[field_name] = clean_value

    try:
        result = _turn_execution_update_one(
            coll,
            scope_query,
            {
                "$set": payload,
                "$setOnInsert": {"inserted_at": now},
            },
            operation="upsert_turn_execution_record_projection.update_one",
            upsert=True,
        )
        updated = bool(getattr(result, "modified_count", 0) > 0)
        inserted = getattr(result, "upserted_id", None) is not None
        matched = bool(getattr(result, "matched_count", 0) > 0)
        return {
            "updated": updated or inserted,
            "inserted": inserted,
            "matched": matched,
            "request_id": request_id,
        }
    except DuplicateKeyError:
        # request_id is globally unique. A scoped upsert that collides with an
        # existing record therefore denotes a different actor scope, not a
        # record this caller may replace.
        logger.warning(
            "Refused turn_execution_records scope collision for request_id=%s",
            request_id,
        )
        return {
            "updated": False,
            "reason": "actor_scope_mismatch",
            "request_id": request_id,
        }
    except PyMongoError as exc:
        logger.warning(
            "Failed to upsert turn_execution_records projection for request_id=%s: %s",
            request_id,
            exc,
        )
        return {
            "updated": False,
            "reason": "mongo_error",
            "request_id": request_id,
        }


def append_late_effect_observation(
    *,
    request_id: Any,
    effect_id: Any,
    execution_id: Any,
    observation: Mapping[str, Any],
    user_id: Any = None,
    session_id: Any = None,
    namespace: Any = None,
    org_id: Any = None,
) -> dict[str, Any]:
    """Append one bounded, identity-bearing late-effect observation.

    The entry is additive to the ordinary turn projection: a later full-record
    ``$set`` upsert does not remove it.  The conditional append is idempotent
    by ``observation_id``, including when a retry changes volatile receipt
    fields.  The caller remains responsible for interpreting a handler receipt
    versus canonical state.

    Entries are individually bounded, but the array has no arbitrary count
    ceiling.  A per-turn retention limit should therefore use an atomic
    pipeline or a separately indexed collection if observed growth
    demonstrates the need.
    """

    clean_request_id = _safe_str(request_id)
    clean_effect_id = _safe_str(effect_id)
    clean_execution_id = _safe_str(execution_id)
    identities = {
        "request_id": clean_request_id,
        "effect_id": clean_effect_id,
        "execution_id": clean_execution_id,
    }
    for field_name, value in identities.items():
        if not value:
            return {"updated": False, "reason": f"missing_{field_name}"}
        if len(value) > _LATE_EFFECT_OBSERVATION_MAX_ID_CHARS:
            return {"updated": False, "reason": f"invalid_{field_name}"}
    if not isinstance(observation, Mapping):
        return {"updated": False, "reason": "invalid_observation"}

    actor_scope: dict[str, str] = {}
    for field_name, raw_value in (
        ("namespace", namespace),
        ("user_id", user_id),
        ("org_id", org_id),
    ):
        clean_value = _safe_str(raw_value)
        if clean_value:
            actor_scope[field_name] = clean_value
    if not actor_scope:
        return {
            "updated": False,
            "reason": "missing_actor_scope",
            "request_id": clean_request_id,
        }

    coll = get_turn_execution_records_collection()
    if coll is None:
        return {
            "updated": False,
            "reason": "collection_unavailable",
            "request_id": clean_request_id,
        }

    source_observation = dict(observation)
    bounded_observation = _compact_final_answer_projection_payload(
        source_observation,
        max_depth=4,
        max_items=32,
        max_string_chars=2_000,
    )
    if not isinstance(bounded_observation, Mapping):
        bounded_observation = {"value": bounded_observation}
    bounded_observation = dict(bounded_observation)
    storage_transformed = _hash_payload(source_observation) != _hash_payload(
        bounded_observation
    )

    observed_at_utc = (
        _safe_str(bounded_observation.get("observed_at_utc"))
        or _iso_utc(_now_utc())
    )
    observation_identity = "\0".join(
        (clean_request_id, clean_effect_id, clean_execution_id)
    )
    observation_id = hashlib.sha256(
        observation_identity.encode("utf-8")
    ).hexdigest()
    entry = dict(bounded_observation)
    entry.update(
        {
            "schema_version": LATE_EFFECT_OBSERVATION_SCHEMA_VERSION,
            "observation_id": observation_id,
            "effect_id": clean_effect_id,
            "execution_id": clean_execution_id,
            "observed_at_utc": observed_at_utc,
            "storage_transformed": storage_transformed,
        }
    )

    now = _now_utc()
    insert_payload: dict[str, Any] = {
        "request_id": clean_request_id,
        "schema_version": TURN_EXECUTION_RECORD_SCHEMA_VERSION,
        "created_at_utc": _iso_utc(now),
        "updated_at_utc": _iso_utc(now),
        "inserted_at": now,
    }
    for field_name, raw_value in (
        ("user_id", user_id),
        ("session_id", session_id),
        ("namespace", namespace),
        ("org_id", org_id),
    ):
        clean_value = _safe_str(raw_value)
        if clean_value:
            insert_payload[field_name] = clean_value

    record_scope_query = {
        "request_id": clean_request_id,
        **actor_scope,
    }
    append_query = {
        **record_scope_query,
        "late_effect_observations": {
            "$not": {"$elemMatch": {"observation_id": observation_id}}
        },
    }
    persisted_at_utc = _iso_utc(now)
    append_update = {
        "$push": {"late_effect_observations": entry},
        "$max": {
            "updated_at_utc": persisted_at_utc,
            "late_effect_updated_at_utc": persisted_at_utc,
        },
    }

    try:
        result = _turn_execution_update_one(
            coll,
            append_query,
            append_update,
            operation="append_late_effect_observation.update_one",
            detail=clean_execution_id,
            upsert=False,
        )
        appended = bool(getattr(result, "modified_count", 0) > 0)
        matched = bool(getattr(result, "matched_count", 0) > 0)
        inserted = False
        if not matched:
            existing = _turn_execution_find_one(
                coll,
                record_scope_query,
                projection={
                    "_id": 0,
                    "late_effect_observations.observation_id": 1,
                },
                operation="append_late_effect_observation.find_existing",
                detail=clean_execution_id,
            )
            existing_observations = (
                existing.get("late_effect_observations")
                if isinstance(existing, Mapping)
                and isinstance(existing.get("late_effect_observations"), list)
                else []
            )
            duplicate = any(
                isinstance(item, Mapping)
                and item.get("observation_id") == observation_id
                for item in existing_observations
            )
            if duplicate:
                return {
                    "updated": False,
                    "appended": False,
                    "duplicate": True,
                    "inserted": False,
                    "matched": True,
                    "request_id": clean_request_id,
                    "effect_id": clean_effect_id,
                    "execution_id": clean_execution_id,
                    "observation_id": observation_id,
                }

            if existing is None:
                request_id_collision = _turn_execution_find_one(
                    coll,
                    {"request_id": clean_request_id},
                    projection={
                        "_id": 0,
                        "namespace": 1,
                        "user_id": 1,
                        "org_id": 1,
                    },
                    operation="append_late_effect_observation.find_scope_collision",
                    detail=clean_execution_id,
                )
                if request_id_collision is not None:
                    return {
                        "updated": False,
                        "appended": False,
                        "duplicate": False,
                        "inserted": False,
                        "matched": False,
                        "reason": "actor_scope_mismatch",
                        "request_id": clean_request_id,
                        "effect_id": clean_effect_id,
                        "execution_id": clean_execution_id,
                        "observation_id": observation_id,
                    }
                ensured = _turn_execution_update_one(
                    coll,
                    record_scope_query,
                    {"$setOnInsert": insert_payload},
                    operation="append_late_effect_observation.ensure_record",
                    detail=clean_execution_id,
                    upsert=True,
                )
                inserted = getattr(ensured, "upserted_id", None) is not None

            # Re-evaluate the identity predicate after the record existence
            # check so concurrent submissions still admit at most one entry.
            result = _turn_execution_update_one(
                coll,
                append_query,
                append_update,
                operation="append_late_effect_observation.retry_update_one",
                detail=clean_execution_id,
                upsert=False,
            )
            appended = bool(getattr(result, "modified_count", 0) > 0)
            matched = bool(getattr(result, "matched_count", 0) > 0)

        duplicate = False
        if not appended and not matched:
            final_existing = _turn_execution_find_one(
                coll,
                record_scope_query,
                projection={
                    "_id": 0,
                    "late_effect_observations.observation_id": 1,
                },
                operation="append_late_effect_observation.confirm_identity",
                detail=clean_execution_id,
            )
            final_observations = (
                final_existing.get("late_effect_observations")
                if isinstance(final_existing, Mapping)
                and isinstance(
                    final_existing.get("late_effect_observations"), list
                )
                else []
            )
            duplicate = any(
                isinstance(item, Mapping)
                and item.get("observation_id") == observation_id
                for item in final_observations
            )
            if not duplicate:
                return {
                    "updated": False,
                    "appended": False,
                    "duplicate": False,
                    "inserted": inserted,
                    "matched": False,
                    "reason": "append_not_acknowledged",
                    "request_id": clean_request_id,
                    "effect_id": clean_effect_id,
                    "execution_id": clean_execution_id,
                    "observation_id": observation_id,
                }

        return {
            "updated": appended,
            "appended": appended,
            "duplicate": duplicate,
            "inserted": inserted,
            "matched": matched,
            "request_id": clean_request_id,
            "effect_id": clean_effect_id,
            "execution_id": clean_execution_id,
            "observation_id": observation_id,
        }
    except PyMongoError as exc:
        logger.warning(
            "Failed to append late effect observation for request_id=%s "
            "effect_id=%s execution_id=%s: %s",
            clean_request_id,
            clean_effect_id,
            clean_execution_id,
            exc,
        )
        return {
            "updated": False,
            "reason": "mongo_error",
            "request_id": clean_request_id,
            "effect_id": clean_effect_id,
            "execution_id": clean_execution_id,
        }


def submit_late_effect_observation(**kwargs: Any) -> bool:
    """Queue a durable append without occupying an MCP handler worker."""

    if not _LATE_EFFECT_APPEND_SLOTS.acquire(blocking=False):
        logger.warning(
            "Late-effect observation queue is full; request_id=%s effect_id=%s",
            _safe_str(kwargs.get("request_id")),
            _safe_str(kwargs.get("effect_id")),
        )
        return False

    def _append() -> None:
        try:
            outcome = append_late_effect_observation(**kwargs)
            if not outcome.get("updated") and not outcome.get("duplicate"):
                logger.warning(
                    "Late-effect observation was not persisted; request_id=%s "
                    "effect_id=%s reason=%s",
                    _safe_str(kwargs.get("request_id")),
                    _safe_str(kwargs.get("effect_id")),
                    _safe_str(outcome.get("reason")) or "append_not_acknowledged",
                )
        except Exception:
            logger.exception(
                "Late-effect observation append failed; request_id=%s "
                "effect_id=%s",
                _safe_str(kwargs.get("request_id")),
                _safe_str(kwargs.get("effect_id")),
            )
        finally:
            _LATE_EFFECT_APPEND_SLOTS.release()

    try:
        _LATE_EFFECT_APPEND_EXECUTOR.submit(_append)
    except RuntimeError:
        _LATE_EFFECT_APPEND_SLOTS.release()
        logger.warning(
            "Late-effect observation executor is unavailable; request_id=%s "
            "effect_id=%s",
            _safe_str(kwargs.get("request_id")),
            _safe_str(kwargs.get("effect_id")),
        )
        return False
    return True


def get_latest_turn_execution_record_projection(
    *,
    session_id: str | None,
    namespace: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest projected turn-execution record for a chat session."""

    clean_session_id = _safe_str(session_id)
    if not clean_session_id:
        return None

    coll = get_turn_execution_records_collection()
    if coll is None:
        return None

    query: dict[str, Any] = {"session_id": clean_session_id}
    clean_namespace = _safe_str(namespace)
    if clean_namespace:
        query["namespace"] = clean_namespace
    clean_user_id = _safe_str(user_id)
    if clean_user_id:
        query["user_id"] = clean_user_id

    projection = {"_id": 0}
    doc = _turn_execution_find_one(
        coll,
        query,
        projection=projection,
        sort=[("created_at_utc", DESCENDING), ("updated_at_utc", DESCENDING)],
        operation="get_latest_turn_execution_record_projection.find_latest",
    )
    if isinstance(doc, Mapping):
        hydrated = hydrate_debug_payload_blob_refs(doc, fail_soft=True)
        return (
            dict(hydrated.payload)
            if isinstance(hydrated.payload, Mapping)
            else dict(doc)
        )

    # Older projections may be missing namespace/user_id even when session_id is
    # stable, so fall back to session-scoped lookup before giving up.
    if clean_namespace or clean_user_id:
        doc = _turn_execution_find_one(
            coll,
            {"session_id": clean_session_id},
            projection=projection,
            sort=[("created_at_utc", DESCENDING), ("updated_at_utc", DESCENDING)],
            operation="get_latest_turn_execution_record_projection.fallback_find_latest",
        )
        if isinstance(doc, Mapping):
            hydrated = hydrate_debug_payload_blob_refs(doc, fail_soft=True)
            return (
                dict(hydrated.payload)
                if isinstance(hydrated.payload, Mapping)
                else dict(doc)
            )

    return None


def get_turn_execution_record_projection(
    *,
    request_id: str | None,
    namespace: str | None = None,
) -> dict[str, Any] | None:
    """Return one projected turn-execution record by request id."""

    clean_request_id = _safe_str(request_id)
    if not clean_request_id:
        return None

    coll = get_turn_execution_records_collection()
    if coll is None:
        return None

    query: dict[str, Any] = {"request_id": clean_request_id}
    clean_namespace = _safe_str(namespace)
    if clean_namespace:
        query["namespace"] = clean_namespace

    doc = _turn_execution_find_one(
        coll,
        query,
        projection={"_id": 0},
        operation="get_turn_execution_record_projection.find_by_request",
    )
    if isinstance(doc, Mapping):
        hydrated = hydrate_debug_payload_blob_refs(doc, fail_soft=True)
        return (
            dict(hydrated.payload)
            if isinstance(hydrated.payload, Mapping)
            else dict(doc)
        )
    return None


def _normalise_positive_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _get_nested_value(payload: Any, path: str) -> Any:
    if not isinstance(payload, Mapping):
        return None
    current: Any = payload
    for raw_part in path.split("."):
        part = raw_part.strip()
        if not part:
            continue
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _safe_percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def _safe_count_documents(collection: Any, query: Mapping[str, Any]) -> int:
    if collection is None:
        return 0
    if hasattr(collection, "count_documents"):
        try:
            return int(collection.count_documents(dict(query)))
        except Exception:
            pass
    try:
        return sum(1 for _ in collection.find(dict(query), {"_id": 1}))
    except Exception:
        return 0


def _normalise_timestamp_for_range(value: Any) -> str | None:
    if isinstance(value, datetime):
        return _iso_utc(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return None


def _append_unique_text(
    target: list[str],
    seen: set[str],
    *,
    value: Any,
    limit: int,
) -> None:
    if len(target) >= limit:
        return
    text = _safe_str(value)
    if not text:
        return
    lowered = text.lower()
    if lowered in seen:
        return
    seen.add(lowered)
    target.append(text)


def _collect_namespace_candidates(
    *,
    chat_history_coll: Any,
    turn_records_coll: Any,
    limit_namespaces: int,
) -> list[str]:
    namespace_items: list[str] = []
    seen: set[str] = set()

    def _collect_from_distinct(collection: Any, field: str) -> None:
        if collection is None or len(namespace_items) >= limit_namespaces:
            return
        if hasattr(collection, "distinct"):
            try:
                values = collection.distinct(field)
            except TypeError:
                try:
                    values = collection.distinct(field, {})
                except Exception:
                    values = None
            except Exception:
                values = None
            if isinstance(values, list):
                for raw_value in values:
                    _append_unique_text(
                        namespace_items,
                        seen,
                        value=raw_value,
                        limit=limit_namespaces,
                    )
                    if len(namespace_items) >= limit_namespaces:
                        return

    def _collect_from_find(collection: Any, field: str) -> None:
        if collection is None or len(namespace_items) >= limit_namespaces:
            return
        try:
            cursor = collection.find({}, {field: 1})
        except Exception:
            return
        for doc in cursor:
            value = _get_nested_value(doc, field)
            _append_unique_text(
                namespace_items,
                seen,
                value=value,
                limit=limit_namespaces,
            )
            if len(namespace_items) >= limit_namespaces:
                return

    _collect_from_distinct(chat_history_coll, "namespace")
    _collect_from_distinct(turn_records_coll, "namespace")
    if not namespace_items:
        _collect_from_find(chat_history_coll, "namespace")
        _collect_from_find(turn_records_coll, "namespace")
    namespace_items.sort()
    return namespace_items


def build_turn_execution_namespace_coverage_report(
    *,
    namespace: str | None = None,
    limit_namespaces: int = 25,
    limit_sessions_per_namespace: int = 200,
    limit_projected_records_per_namespace: int = 10000,
) -> dict[str, Any]:
    """Build namespace-level turn execution coverage metrics.

    This report compares assistant-message instrumentation in chat history
    against projected turn_execution_records so reliability gaps can be
    quantified before failure-mining benchmarks are interpreted.
    """

    namespace_filter = _safe_str(namespace)
    namespaces_limit = _normalise_positive_int(
        limit_namespaces,
        default=25,
        minimum=1,
        maximum=500,
    )
    session_limit = _normalise_positive_int(
        limit_sessions_per_namespace,
        default=200,
        minimum=1,
        maximum=10000,
    )
    projected_limit = _normalise_positive_int(
        limit_projected_records_per_namespace,
        default=10000,
        minimum=1,
        maximum=200000,
    )

    db = get_db()
    if db is None:
        return {"success": False, "error": "db_unavailable"}

    try:
        chat_history_coll = db["chat_history"]
        turn_records_coll = db[TURN_EXECUTION_RECORDS_COLLECTION]
    except Exception:
        return {"success": False, "error": "collection_unavailable"}

    if namespace_filter:
        namespaces = [namespace_filter]
    else:
        namespaces = _collect_namespace_candidates(
            chat_history_coll=chat_history_coll,
            turn_records_coll=turn_records_coll,
            limit_namespaces=namespaces_limit,
        )

    namespace_reports: list[dict[str, Any]] = []
    aggregate_assistant = 0
    aggregate_embedded = 0
    aggregate_debug = 0
    aggregate_projected = 0
    aggregate_overlap = 0
    aggregate_history_request_ids = 0
    capability_gap_index: dict[str, dict[str, Any]] = {}

    for namespace_value in namespaces[:namespaces_limit]:
        session_query = {"namespace": namespace_value}
        session_projection = {
            "user_id": 1,
            "session_id": 1,
            "history.role": 1,
            "history.timestamp": 1,
            "history.llm_debug_data": 1,
        }
        try:
            session_cursor = chat_history_coll.find(
                session_query, session_projection
            ).limit(session_limit)
        except Exception:
            session_cursor = []

        sessions_scanned = 0
        assistant_messages_scanned = 0
        assistant_messages_with_llm_debug_data = 0
        assistant_messages_with_request_id = 0
        assistant_messages_with_turn_execution_record = 0
        history_request_ids: set[str] = set()
        earliest_assistant_timestamp: str | None = None
        latest_assistant_timestamp: str | None = None

        for session_doc in session_cursor:
            if not isinstance(session_doc, Mapping):
                continue
            sessions_scanned += 1
            history_items = session_doc.get("history")
            if not isinstance(history_items, list):
                continue

            for message in history_items:
                if not isinstance(message, Mapping):
                    continue
                if message.get("role") != "assistant":
                    continue
                assistant_messages_scanned += 1

                timestamp_value = _normalise_timestamp_for_range(
                    message.get("timestamp")
                )
                if timestamp_value:
                    if (
                        earliest_assistant_timestamp is None
                        or timestamp_value < earliest_assistant_timestamp
                    ):
                        earliest_assistant_timestamp = timestamp_value
                    if (
                        latest_assistant_timestamp is None
                        or timestamp_value > latest_assistant_timestamp
                    ):
                        latest_assistant_timestamp = timestamp_value

                llm_debug_data = message.get("llm_debug_data")
                if not isinstance(llm_debug_data, Mapping):
                    continue
                assistant_messages_with_llm_debug_data += 1

                debug_request_id = _safe_str(llm_debug_data.get("request_id"))
                if debug_request_id:
                    assistant_messages_with_request_id += 1
                    history_request_ids.add(debug_request_id)

                turn_record = llm_debug_data.get("turn_execution_record")
                if not isinstance(turn_record, Mapping):
                    continue
                assistant_messages_with_turn_execution_record += 1

                record_request_id = _safe_str(turn_record.get("request_id"))
                if record_request_id:
                    history_request_ids.add(record_request_id)

        projected_query = {"namespace": namespace_value}
        projected_records_total = _safe_count_documents(
            turn_records_coll, projected_query
        )
        projected_projection = {
            "request_id": 1,
            "created_at_utc": 1,
            "completion_gate.decision": 1,
            "workflow_selection.selected_workflow_id": 1,
        }
        try:
            projected_cursor = turn_records_coll.find(
                projected_query, projected_projection
            ).limit(projected_limit)
        except Exception:
            projected_cursor = []

        projected_records_scanned = 0
        projected_records_missing_request_id = 0
        projected_records_missing_decision = 0
        projected_records_missing_workflow = 0
        projected_request_ids: set[str] = set()
        projected_created_min: str | None = None
        projected_created_max: str | None = None

        for projected_doc in projected_cursor:
            if not isinstance(projected_doc, Mapping):
                continue
            projected_records_scanned += 1

            request_id = _safe_str(projected_doc.get("request_id"))
            if request_id:
                projected_request_ids.add(request_id)
            else:
                projected_records_missing_request_id += 1

            decision = _safe_str(
                _get_nested_value(projected_doc, "completion_gate.decision")
            )
            if not decision:
                projected_records_missing_decision += 1

            workflow_id = _safe_str(
                _get_nested_value(
                    projected_doc,
                    "workflow_selection.selected_workflow_id",
                )
            )
            if not workflow_id:
                projected_records_missing_workflow += 1

            created_at_utc = _normalise_timestamp_for_range(
                projected_doc.get("created_at_utc")
            )
            if created_at_utc:
                if (
                    projected_created_min is None
                    or created_at_utc < projected_created_min
                ):
                    projected_created_min = created_at_utc
                if (
                    projected_created_max is None
                    or created_at_utc > projected_created_max
                ):
                    projected_created_max = created_at_utc

        overlap_count = len(history_request_ids.intersection(projected_request_ids))
        history_request_id_count = len(history_request_ids)

        namespace_gaps: list[dict[str, Any]] = []
        if (
            assistant_messages_scanned > 0
            and assistant_messages_with_turn_execution_record == 0
        ):
            namespace_gaps.append(
                {
                    "gap_id": "no_embedded_turn_execution_record_in_history",
                    "severity": "high",
                    "description": (
                        "Assistant turns exist but none include llm_debug_data.turn_execution_record."
                    ),
                }
            )
        if assistant_messages_scanned > 0 and projected_records_total == 0:
            namespace_gaps.append(
                {
                    "gap_id": "no_turn_execution_records_projection",
                    "severity": "high",
                    "description": (
                        "No projected turn_execution_records were found for this namespace."
                    ),
                }
            )
        if history_request_id_count > 0 and overlap_count == 0:
            namespace_gaps.append(
                {
                    "gap_id": "no_request_id_overlap_between_history_and_projection",
                    "severity": "medium",
                    "description": (
                        "History request IDs and projected request IDs did not overlap in the sampled window."
                    ),
                }
            )
        if projected_records_scanned > 0 and projected_records_missing_decision > 0:
            namespace_gaps.append(
                {
                    "gap_id": "projection_missing_completion_decision",
                    "severity": "medium",
                    "description": (
                        "Some projected records are missing completion_gate.decision."
                    ),
                    "missing_count": projected_records_missing_decision,
                }
            )
        if projected_records_scanned > 0 and projected_records_missing_workflow > 0:
            namespace_gaps.append(
                {
                    "gap_id": "projection_missing_selected_workflow_id",
                    "severity": "low",
                    "description": (
                        "Some projected records are missing workflow_selection.selected_workflow_id."
                    ),
                    "missing_count": projected_records_missing_workflow,
                }
            )

        for gap in namespace_gaps:
            gap_id = _safe_str(gap.get("gap_id"))
            if not gap_id:
                continue
            bucket = capability_gap_index.setdefault(
                gap_id,
                {
                    "gap_id": gap_id,
                    "severity": _safe_str(gap.get("severity")) or "medium",
                    "title": gap_id.replace("_", " "),
                    "description": _safe_str(gap.get("description")) or "",
                    "evidence_count": 0,
                    "namespaces": [],
                },
            )
            bucket["evidence_count"] = int(bucket.get("evidence_count", 0)) + 1
            namespaces_with_gap = bucket.get("namespaces")
            if isinstance(namespaces_with_gap, list):
                if namespace_value not in namespaces_with_gap:
                    namespaces_with_gap.append(namespace_value)

        namespace_report = {
            "namespace": namespace_value,
            "sessions_scanned": sessions_scanned,
            "assistant_messages_scanned": assistant_messages_scanned,
            "assistant_messages_with_llm_debug_data": assistant_messages_with_llm_debug_data,
            "assistant_messages_with_request_id": assistant_messages_with_request_id,
            "assistant_messages_with_turn_execution_record": (
                assistant_messages_with_turn_execution_record
            ),
            "history_request_id_count": history_request_id_count,
            "projected_records_total": projected_records_total,
            "projected_records_scanned": projected_records_scanned,
            "projected_records_scan_truncated": projected_records_total
            > projected_records_scanned,
            "projected_records_missing_request_id": projected_records_missing_request_id,
            "projected_records_missing_completion_decision": projected_records_missing_decision,
            "projected_records_missing_selected_workflow_id": projected_records_missing_workflow,
            "request_id_overlap_count": overlap_count,
            "assistant_embedded_record_rate_pct": _safe_percentage(
                assistant_messages_with_turn_execution_record,
                assistant_messages_scanned,
            ),
            "assistant_llm_debug_rate_pct": _safe_percentage(
                assistant_messages_with_llm_debug_data,
                assistant_messages_scanned,
            ),
            "request_id_overlap_rate_pct": _safe_percentage(
                overlap_count,
                history_request_id_count,
            ),
            "assistant_message_timestamp_range": {
                "earliest": earliest_assistant_timestamp,
                "latest": latest_assistant_timestamp,
            },
            "projected_created_at_utc_range": {
                "earliest": projected_created_min,
                "latest": projected_created_max,
            },
            "gap_signals": namespace_gaps,
        }
        namespace_reports.append(namespace_report)

        aggregate_assistant += assistant_messages_scanned
        aggregate_embedded += assistant_messages_with_turn_execution_record
        aggregate_debug += assistant_messages_with_llm_debug_data
        aggregate_projected += projected_records_total
        aggregate_overlap += overlap_count
        aggregate_history_request_ids += history_request_id_count

    capability_gaps = sorted(
        capability_gap_index.values(),
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(
                _safe_str(item.get("severity")) or "medium", 3
            ),
            -int(item.get("evidence_count", 0)),
            _safe_str(item.get("gap_id")) or "",
        ),
    )

    if not namespaces and not namespace_filter:
        capability_gaps.append(
            {
                "gap_id": "no_namespaces_found",
                "severity": "high",
                "title": "No namespaces found",
                "description": (
                    "No namespaces were discovered in chat_history or turn_execution_records."
                ),
                "evidence_count": 0,
                "namespaces": [],
            }
        )

    return {
        "success": True,
        "generated_at_utc": _iso_utc(),
        "namespace_filter": namespace_filter,
        "limit_namespaces": namespaces_limit,
        "limit_sessions_per_namespace": session_limit,
        "limit_projected_records_per_namespace": projected_limit,
        "namespaces_scanned": len(namespace_reports),
        "coverage_by_namespace": namespace_reports,
        "aggregate": {
            "assistant_messages_scanned": aggregate_assistant,
            "assistant_messages_with_llm_debug_data": aggregate_debug,
            "assistant_messages_with_turn_execution_record": aggregate_embedded,
            "projected_records_total": aggregate_projected,
            "history_request_id_count": aggregate_history_request_ids,
            "request_id_overlap_count": aggregate_overlap,
            "assistant_embedded_record_rate_pct": _safe_percentage(
                aggregate_embedded, aggregate_assistant
            ),
            "assistant_llm_debug_rate_pct": _safe_percentage(
                aggregate_debug, aggregate_assistant
            ),
            "request_id_overlap_rate_pct": _safe_percentage(
                aggregate_overlap, aggregate_history_request_ids
            ),
        },
        "capability_gaps": capability_gaps,
    }


def infer_turn_execution_workflow_routing_from_debug(
    *, llm_debug: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    if not isinstance(llm_debug, Mapping):
        return None
    existing = llm_debug.get("workflow_routing")
    if isinstance(existing, Mapping):
        cleaned: dict[str, Any] = {}
        for key in ("workflow_id", "verdict", "source"):
            value = _safe_str(existing.get(key))
            if value:
                cleaned[key] = value
        if cleaned:
            return cleaned

    tool_invocations = (
        llm_debug.get("tool_invocations")
        if isinstance(llm_debug.get("tool_invocations"), list)
        else []
    )
    if tool_invocations:
        has_write_tools = False
        for raw_invocation in tool_invocations:
            if not isinstance(raw_invocation, Mapping):
                continue
            if _is_write_tool(_safe_str(raw_invocation.get("name"))):
                has_write_tools = True
                break
        return {
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "mutation_tooling" if has_write_tools else "tool_seeking",
            "source": "synthesised",
        }

    return {
        "workflow_id": "#V#chat_assistant_workflow",
        "verdict": "plain_response",
        "source": "synthesised",
    }


def backfill_turn_execution_records_from_chat_history(
    *,
    namespace: str,
    limit_sessions: int = 500,
    dry_run: bool = True,
    synthesise_missing_records: bool = True,
) -> dict[str, Any]:
    """Backfill turn_execution_records from chat_history assistant messages.

    This scans sessions in the provided namespace and extracts
    `history[].llm_debug_data.turn_execution_record` payloads for assistant turns.
    When embedded records are missing, optional synthesis can reconstruct records
    from legacy llm_debug_data + message context. By default this runs in dry-run
    mode to report potential backfill volume without mutating Mongo.
    """

    namespace_value = _safe_str(namespace)
    if not namespace_value:
        return {"success": False, "error": "namespace_required"}

    session_limit = _normalise_positive_int(
        limit_sessions,
        default=500,
        minimum=1,
        maximum=5000,
    )
    run_dry = bool(dry_run)
    run_synthesis = bool(synthesise_missing_records)

    db = get_db()
    if db is None:
        return {"success": False, "error": "db_unavailable"}

    try:
        chat_history_coll = db["chat_history"]
    except Exception:
        return {"success": False, "error": "chat_history_collection_unavailable"}

    query = {"namespace": namespace_value}
    projection = {
        "user_id": 1,
        "session_id": 1,
        "organisation_concept_id": 1,
        "history.role": 1,
        "history.content": 1,
        "history.timestamp": 1,
        "history.llm_debug_data": 1,
    }
    cursor = chat_history_coll.find(query, projection).limit(session_limit)

    sessions_scanned = 0
    assistant_messages_scanned = 0
    assistant_messages_with_llm_debug_data = 0
    assistant_messages_with_request_id = 0
    records_found = 0
    embedded_records_found = 0
    synthesised_records_found = 0
    candidate_records = 0
    upserted_count = 0
    inserted_count = 0
    skipped_missing_request_id = 0
    skipped_invalid_record = 0
    skipped_missing_user_or_session = 0
    failure_count = 0
    skipped_reasons: dict[str, int] = {}
    example_request_ids: list[str] = []

    for session_doc in cursor:
        if not isinstance(session_doc, Mapping):
            continue
        sessions_scanned += 1
        user_id = _safe_str(session_doc.get("user_id"))
        session_id = _safe_str(session_doc.get("session_id"))
        org_id = _safe_str(session_doc.get("organisation_concept_id"))
        history = session_doc.get("history")
        if not isinstance(history, list):
            continue

        latest_user_prompt: str | None = None
        for message in history:
            if not isinstance(message, Mapping):
                continue
            role = message.get("role")
            if role == "user":
                latest_user_prompt = (
                    _safe_str(message.get("content")) or latest_user_prompt
                )
                continue
            if role != "assistant":
                continue
            assistant_messages_scanned += 1

            llm_debug = message.get("llm_debug_data")
            if not isinstance(llm_debug, Mapping):
                continue
            assistant_messages_with_llm_debug_data += 1

            debug_request_id = _safe_str(llm_debug.get("request_id"))
            if debug_request_id:
                assistant_messages_with_request_id += 1
            tool_invocations = (
                llm_debug.get("tool_invocations")
                if isinstance(llm_debug.get("tool_invocations"), list)
                else []
            )
            workflow_routing = infer_turn_execution_workflow_routing_from_debug(
                llm_debug=llm_debug
            )

            record = llm_debug.get("turn_execution_record")
            record_source = "embedded"
            if not isinstance(record, Mapping):
                if not run_synthesis or not debug_request_id:
                    continue
                try:
                    actor_concept_id = _safe_str(llm_debug.get("actor_concept_id"))
                    record = build_turn_execution_record(
                        request_id=debug_request_id,
                        session_id=session_id,
                        namespace=namespace_value,
                        actor_concept_id=actor_concept_id,
                        user_id=user_id,
                        org_id=org_id,
                        prompt_text=latest_user_prompt,
                        response_text=_safe_str(message.get("content")),
                        interaction_timestamp_utc=(
                            llm_debug.get("interaction_timestamp_utc")
                            or message.get("timestamp")
                        ),
                        workflow_discovery=(
                            llm_debug.get("workflow_discovery")
                            if isinstance(llm_debug.get("workflow_discovery"), Mapping)
                            else None
                        ),
                        workflow_routing=workflow_routing,
                        tool_invocations=tool_invocations,
                        turn_execution_diagnostics=(
                            llm_debug.get("turn_execution_diagnostics")
                            if isinstance(
                                llm_debug.get("turn_execution_diagnostics"), Mapping
                            )
                            else None
                        ),
                        aux_llm_calls=(
                            llm_debug.get("aux_llm_calls")
                            if isinstance(llm_debug.get("aux_llm_calls"), list)
                            else []
                        ),
                    )
                    if isinstance(record, dict):
                        record["reconstruction"] = {
                            "source": "chat_history.llm_debug_data",
                            "method": "build_turn_execution_record",
                            "synthesised_at_utc": _iso_utc(),
                        }
                    record_source = "synthesised_from_llm_debug_data"
                except Exception:
                    skipped_reasons["record_synthesis_failed"] = (
                        skipped_reasons.get("record_synthesis_failed", 0) + 1
                    )
                    failure_count += 1
                    continue

            if not isinstance(record, Mapping):
                continue
            if isinstance(record, dict):
                workflow_selection = record.get("workflow_selection")
                if not isinstance(workflow_selection, dict):
                    workflow_selection = {}
                    record["workflow_selection"] = workflow_selection
                if not _safe_str(
                    workflow_selection.get("selected_workflow_id")
                ) and isinstance(workflow_routing, Mapping):
                    inferred_workflow_id = _safe_str(
                        workflow_routing.get("workflow_id")
                    )
                    inferred_verdict = _safe_str(workflow_routing.get("verdict"))
                    inferred_source = _safe_str(workflow_routing.get("source"))
                    if inferred_workflow_id:
                        workflow_selection["selected_workflow_id"] = (
                            inferred_workflow_id
                        )
                    if inferred_verdict:
                        workflow_selection["selector_verdict"] = inferred_verdict
                    if inferred_source:
                        workflow_selection["selector_source"] = inferred_source
            records_found += 1
            if record_source == "embedded":
                embedded_records_found += 1
            else:
                synthesised_records_found += 1

            request_id = _safe_str(record.get("request_id"))
            if not request_id:
                skipped_missing_request_id += 1
                skipped_reasons["missing_request_id"] = (
                    skipped_reasons.get("missing_request_id", 0) + 1
                )
                continue

            if request_id not in example_request_ids:
                example_request_ids.append(request_id)

            if not user_id or not session_id:
                skipped_missing_user_or_session += 1
                skipped_reasons["missing_user_or_session"] = (
                    skipped_reasons.get("missing_user_or_session", 0) + 1
                )
                continue

            candidate_records += 1
            if run_dry:
                continue

            outcome = upsert_turn_execution_record_projection(
                record=record,
                user_id=user_id,
                session_id=session_id,
                namespace=namespace_value,
                org_id=org_id,
            )
            if bool(outcome.get("updated", False)):
                try:
                    from .episode_critique_memory_service import (
                        upsert_episode_critique_memory_from_turn,
                    )

                    upsert_episode_critique_memory_from_turn(
                        record=record,
                        llm_debug_data=llm_debug,
                        user_id=user_id,
                        session_id=session_id,
                        namespace=namespace_value,
                        org_id=org_id,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "Failed to backfill episode_critique_memory for request_id=%s: %s",
                        request_id,
                        exc,
                    )
                upserted_count += 1
                if bool(outcome.get("inserted", False)):
                    inserted_count += 1
                continue

            reason = _safe_str(outcome.get("reason")) or "unknown"
            if reason == "invalid_record":
                skipped_invalid_record += 1
            elif reason == "missing_request_id":
                skipped_missing_request_id += 1
            elif reason in {"mongo_error", "collection_unavailable"}:
                failure_count += 1
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1

    gap_signals: list[dict[str, Any]] = []
    if sessions_scanned > 0 and assistant_messages_scanned > 0 and records_found == 0:
        gap_signals.append(
            {
                "gap_id": "no_embedded_turn_execution_record_in_history",
                "severity": "high",
                "description": (
                    "Assistant messages were present but none contained "
                    "recoverable turn execution records in llm_debug_data."
                ),
                "assistant_messages_scanned": assistant_messages_scanned,
            }
        )
    if embedded_records_found == 0 and synthesised_records_found > 0:
        gap_signals.append(
            {
                "gap_id": "embedded_turn_execution_records_missing_recovered_by_synthesis",
                "severity": "medium",
                "description": (
                    "Embedded turn_execution_record payloads were absent, but record "
                    "synthesis from llm_debug_data recovered candidate records."
                ),
                "synthesised_records_found": synthesised_records_found,
            }
        )
    if records_found > 0 and candidate_records == 0:
        gap_signals.append(
            {
                "gap_id": "no_backfill_candidates_after_validation",
                "severity": "medium",
                "description": (
                    "Turn execution records were found in history but none qualified for "
                    "upsert (likely missing request_id or user/session context)."
                ),
                "records_found": records_found,
            }
        )

    return {
        "success": True,
        "namespace": namespace_value,
        "dry_run": run_dry,
        "limit_sessions": session_limit,
        "synthesise_missing_records": run_synthesis,
        "sessions_scanned": sessions_scanned,
        "assistant_messages_scanned": assistant_messages_scanned,
        "assistant_messages_with_llm_debug_data": assistant_messages_with_llm_debug_data,
        "assistant_messages_with_request_id": assistant_messages_with_request_id,
        "records_found": records_found,
        "embedded_records_found": embedded_records_found,
        "synthesised_records_found": synthesised_records_found,
        "candidate_records": candidate_records,
        "upserted_count": upserted_count,
        "inserted_count": inserted_count,
        "skipped_missing_request_id": skipped_missing_request_id,
        "skipped_invalid_record": skipped_invalid_record,
        "skipped_missing_user_or_session": skipped_missing_user_or_session,
        "failure_count": failure_count,
        "skipped_reasons": skipped_reasons,
        "example_request_ids": example_request_ids[:20],
        "gap_signals": gap_signals,
    }
