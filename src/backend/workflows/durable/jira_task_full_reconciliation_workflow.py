"""Reusable action support for the Vontology-authored Jira reconciliation workflow.

The workflow definition itself lives in Vontology. This module only provides
the minimal reusable control surface that VWL currently lacks: a deterministic
gap-block scan over Jira issue keys versus imported Von task keys.
"""

from __future__ import annotations

from typing import Any

from ...services.jira_task_migration_runner_service import (
    DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY,
)
from ...services.jira_task_reconciliation_service import (
    JiraTaskGapScanOptions,
    scan_next_jira_task_gap_block_sync,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)

JIRA_TASK_FULL_RECONCILIATION_WORKFLOW_ID = "#V#jira_task_full_reconciliation_workflow"
JIRA_TASK_SCAN_NEXT_GAP_BLOCK_ACTION_CONCEPT_ID = (
    "#V#jira_task_scan_next_gap_block_action"
)
JIRA_TASK_SCAN_NEXT_GAP_BLOCK_ACTION_ID = "jira_task_scan_next_gap_block_action"


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _context_value(request: WorkflowActionRequest, key: str) -> Any:
    if key in request.inputs:
        return request.inputs.get(key)
    return request.data.get(key)


def _build_gap_scan_options_from_request(
    request: WorkflowActionRequest,
) -> JiraTaskGapScanOptions:
    organisation_concept_id = _clean_text(
        _context_value(request, "organisation_concept_id")
    ) or None

    return JiraTaskGapScanOptions(
        project_key=(
            _clean_text(_context_value(request, "project_key"))
            or DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY
        ),
        organisation_concept_id=organisation_concept_id,
        jql=_clean_text(_context_value(request, "jql")) or None,
        page_size=_coerce_int(_context_value(request, "page_size")) or 100,
        gap_block_size=_coerce_int(_context_value(request, "gap_block_size")) or 100,
        include_done=_coerce_bool(
            _context_value(request, "include_done"),
            default=True,
        ),
    )


def _handle_scan_next_gap_block(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    scan_result = scan_next_jira_task_gap_block_sync(
        _build_gap_scan_options_from_request(request)
    )
    selected_gap_block = scan_result.get("selected_gap_block")
    if not isinstance(selected_gap_block, dict):
        selected_gap_block = {}

    return WorkflowActionResult(
        status="success",
        outputs={
            "result": bool(scan_result.get("gap_block_found")),
            "jira_reconciliation_gap_scan_result": scan_result,
            "jira_reconciliation_gap_block_found": bool(
                scan_result.get("gap_block_found")
            ),
            "jira_reconciliation_gap_block_min_issue_number": (
                selected_gap_block.get("min_issue_number")
            ),
            "jira_reconciliation_gap_block_max_issue_number": (
                selected_gap_block.get("max_issue_number")
            ),
            "min_issue_number": selected_gap_block.get("min_issue_number"),
            "max_issue_number": selected_gap_block.get("max_issue_number"),
            "jira_reconciliation_gap_block_issue_count": (
                selected_gap_block.get("issue_count")
            ),
            "jira_reconciliation_gap_block_issue_keys_sample": (
                selected_gap_block.get("issue_keys") or []
            )[:20],
            "jira_reconciliation_gap_missing_issue_count": (
                scan_result.get("missing_issue_count")
            ),
            "jira_reconciliation_gap_remaining_block_count": (
                scan_result.get("remaining_block_count_after_selected_block")
            ),
            "jira_reconciliation_highest_observed_issue_number": (
                scan_result.get("highest_observed_issue_number")
            ),
        },
    )


def register_jira_task_full_reconciliation_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=JIRA_TASK_SCAN_NEXT_GAP_BLOCK_ACTION_ID,
            handler=_handle_scan_next_gap_block,
            description=(
                "Scan Jira task coverage versus imported Von tasks and choose the "
                "next bounded missing issue-number block."
            ),
            concept_id=JIRA_TASK_SCAN_NEXT_GAP_BLOCK_ACTION_CONCEPT_ID,
            side_effects="read",
        )
    )


__all__ = [
    "JIRA_TASK_FULL_RECONCILIATION_WORKFLOW_ID",
    "JIRA_TASK_SCAN_NEXT_GAP_BLOCK_ACTION_CONCEPT_ID",
    "JIRA_TASK_SCAN_NEXT_GAP_BLOCK_ACTION_ID",
    "register_jira_task_full_reconciliation_actions",
]
