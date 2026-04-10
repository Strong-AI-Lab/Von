"""Generic workflow-authored terminal-success contracts for parent pipelines.

These contracts let Vontology-authored workflows declare which terminal status
counts as success and which execution-summary fields must be reported back to a
parent workflow or turn pipeline before completion can be claimed.
"""

from __future__ import annotations

from typing import Any, Mapping

WORKFLOW_TERMINAL_SUCCESS_CONTRACT_SCHEMA_VERSION = (
    "workflow_terminal_success_contract.v1"
)
WORKFLOW_TERMINAL_SUCCESS_EVALUATION_SCHEMA_VERSION = (
    "workflow_terminal_success_evaluation.v1"
)


def _normalise_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _normalise_text(item)
        lowered = text.lower()
        if not text or lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(text)
    return ordered


def _normalise_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = _normalise_text(value).lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def normalise_workflow_terminal_success_contract(
    value: Any,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("workflow_terminal_success_contract_not_object")

    success_statuses = _normalise_string_list(
        value.get("success_statuses")
        if "success_statuses" in value
        else (
            value.get("accepted_terminal_statuses")
            or value.get("allowed_success_statuses")
            or value.get("success_terminal_statuses")
        )
    )
    if not success_statuses:
        success_statuses = ["completed"]

    required_summary_fields = _normalise_string_list(
        value.get("required_summary_fields")
        if "required_summary_fields" in value
        else (
            value.get("required_report_fields")
            or ["workflow_id", "terminal_status", "final_state", "completed"]
        )
    )
    if not required_summary_fields:
        required_summary_fields = [
            "workflow_id",
            "terminal_status",
            "final_state",
            "completed",
        ]

    return {
        "schema_version": WORKFLOW_TERMINAL_SUCCESS_CONTRACT_SCHEMA_VERSION,
        "success_statuses": success_statuses,
        "required_summary_fields": required_summary_fields,
        "require_terminal_status": _normalise_bool(
            value.get("require_terminal_status"),
            default=True,
        ),
        "require_final_state": _normalise_bool(
            value.get("require_final_state"),
            default=True,
        ),
        "require_completed_true": _normalise_bool(
            value.get("require_completed_true"),
            default=True,
        ),
        "require_completion_gate_safe_to_claim_completion": _normalise_bool(
            value.get("require_completion_gate_safe_to_claim_completion"),
            default=False,
        ),
    }


def _summary_field_present(summary: Mapping[str, Any], field_name: str) -> bool:
    if field_name not in summary:
        return False
    value = summary.get(field_name)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def evaluate_workflow_terminal_success_contract(
    *,
    contract: Mapping[str, Any] | None,
    execution_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(contract, Mapping):
        return None

    summary = execution_summary if isinstance(execution_summary, Mapping) else {}
    success_statuses = _normalise_string_list(contract.get("success_statuses"))
    success_statuses_lower = {item.lower() for item in success_statuses}
    required_summary_fields = _normalise_string_list(
        contract.get("required_summary_fields")
    )
    terminal_status = _normalise_text(summary.get("terminal_status")) or None
    final_state = _normalise_text(summary.get("final_state")) or None
    completed_raw = summary.get("completed")
    completed_present = "completed" in summary and isinstance(completed_raw, bool)
    completion_gate_safe = summary.get("completion_gate_safe_to_claim_completion")

    failure_codes: list[str] = []
    required_fields_missing = [
        field_name
        for field_name in required_summary_fields
        if not _summary_field_present(summary, field_name)
    ]
    if required_fields_missing:
        failure_codes.append("contracted_workflow_summary_fields_missing")

    if bool(contract.get("require_terminal_status", True)):
        if not terminal_status:
            failure_codes.append("contracted_workflow_terminal_status_missing")
        elif terminal_status.lower() not in success_statuses_lower:
            failure_codes.append("contracted_workflow_terminal_status_unexpected")

    if bool(contract.get("require_final_state", True)) and not final_state:
        failure_codes.append("contracted_workflow_final_state_missing")

    if bool(contract.get("require_completed_true", True)):
        if not completed_present:
            failure_codes.append("contracted_workflow_completed_flag_missing")
        elif completed_raw is not True:
            failure_codes.append("contracted_workflow_completed_flag_false")

    if bool(contract.get("require_completion_gate_safe_to_claim_completion", False)):
        if completion_gate_safe is not True:
            failure_codes.append("contracted_workflow_completion_gate_not_safe")

    seen_codes: set[str] = set()
    ordered_failure_codes: list[str] = []
    for code in failure_codes:
        lowered = code.lower()
        if lowered in seen_codes:
            continue
        seen_codes.add(lowered)
        ordered_failure_codes.append(code)

    success = not ordered_failure_codes
    if success:
        decision_reason = "Contracted workflow reported a terminal success state."
    elif "contracted_workflow_terminal_status_missing" in ordered_failure_codes:
        decision_reason = "Contracted workflow did not report a terminal status."
    elif "contracted_workflow_terminal_status_unexpected" in ordered_failure_codes:
        decision_reason = (
            f"Contracted workflow terminal status {terminal_status or 'missing'} "
            "is not listed as a success status."
        )
    elif "contracted_workflow_completion_gate_not_safe" in ordered_failure_codes:
        decision_reason = (
            "Contracted workflow completion gate did not mark the result safe to claim."
        )
    else:
        decision_reason = (
            "Contracted workflow terminal-success contract requirements were not met."
        )

    return {
        "schema_version": WORKFLOW_TERMINAL_SUCCESS_EVALUATION_SCHEMA_VERSION,
        "contract_schema_version": _normalise_text(contract.get("schema_version"))
        or WORKFLOW_TERMINAL_SUCCESS_CONTRACT_SCHEMA_VERSION,
        "success": success,
        "terminal_status": terminal_status,
        "success_statuses": success_statuses,
        "required_summary_fields": required_summary_fields,
        "required_fields_missing": required_fields_missing,
        "completed": completed_raw if isinstance(completed_raw, bool) else None,
        "final_state": final_state,
        "completion_gate_safe_to_claim_completion": (
            completion_gate_safe if isinstance(completion_gate_safe, bool) else None
        ),
        "failure_codes": ordered_failure_codes,
        "decision_reason": decision_reason,
    }


__all__ = [
    "WORKFLOW_TERMINAL_SUCCESS_CONTRACT_SCHEMA_VERSION",
    "WORKFLOW_TERMINAL_SUCCESS_EVALUATION_SCHEMA_VERSION",
    "evaluate_workflow_terminal_success_contract",
    "normalise_workflow_terminal_success_contract",
]
