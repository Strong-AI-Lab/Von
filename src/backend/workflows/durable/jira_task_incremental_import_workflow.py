"""Durable workflow for scheduled Jira task incremental import (JVNAUTOSCI-1407).

The workflow stays deliberately thin. It delegates the heavy lifting to the
shared Jira task migration runner so manual CLI runs, ad-hoc workflow
instances, and scheduled automation all observe the same behaviour.
"""

from __future__ import annotations

from typing import Any

from ...services.jira_task_migration_runner_service import (
    DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY,
    JiraTaskMigrationOptions,
    run_jira_task_migration_sync,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..workflow_registry import WorkflowRegistration

JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID = "#V#jira_task_incremental_import_workflow"
JIRA_TASK_INCREMENTAL_IMPORT_ACTION_ID = "jira_task_incremental_import.run_sync"
JIRA_TASK_INCREMENTAL_IMPORT_VERSION = "jira_task_incremental_import.v1"


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


def _build_options_from_request(
    request: WorkflowActionRequest,
) -> JiraTaskMigrationOptions:
    actor_concept_id = _clean_text(_context_value(request, "actor_concept_id"))
    if not actor_concept_id:
        raise ValueError("actor_concept_id is required for Jira task import workflow")

    namespace = _clean_text(_context_value(request, "namespace")) or _clean_text(
        getattr(request.environment, "user_namespace", None)
    )
    organisation_concept_id = _clean_text(
        _context_value(request, "organisation_concept_id")
    ) or None

    return JiraTaskMigrationOptions(
        actor_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace or None,
        project_key=(
            _clean_text(_context_value(request, "project_key"))
            or DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY
        ),
        jql=_clean_text(_context_value(request, "jql")) or None,
        batch_size=_coerce_int(_context_value(request, "batch_size")) or 50,
        page_size=_coerce_int(_context_value(request, "page_size")) or 100,
        passes=_coerce_int(_context_value(request, "passes")) or 1,
        dry_run=_coerce_bool(_context_value(request, "dry_run"), default=False),
        only_missing=_coerce_bool(
            _context_value(request, "only_missing"),
            default=False,
        ),
        import_referenced_targets=_coerce_bool(
            _context_value(request, "import_referenced_targets"),
            default=True,
        ),
        sync_source_labels=_coerce_bool(
            _context_value(request, "sync_source_labels"),
            default=False,
        ),
        source_migrated_label=(
            _clean_text(_context_value(request, "source_migrated_label"))
            or "migrated"
        ),
        report_path=None,
        updated_within_hours=_coerce_int(
            _context_value(request, "updated_within_hours")
        ),
        include_done=_coerce_bool(
            _context_value(request, "include_done"),
            default=False,
        ),
        min_issue_number=_coerce_int(
            _context_value(request, "min_issue_number")
        ),
        max_issue_number=_coerce_int(
            _context_value(request, "max_issue_number")
        ),
    )


def _handle_run_sync(request: WorkflowActionRequest) -> WorkflowActionResult:
    report = run_jira_task_migration_sync(_build_options_from_request(request))
    current_scope = report.get("current_scope")
    if not isinstance(current_scope, dict):
        current_scope = {}
    return WorkflowActionResult(
        status="success",
        outputs={
            "jira_task_migration_result": report,
            "jira_task_migration_summary": current_scope.get("summary") or {},
            "jira_task_migration_missing_target_issue_keys": (
                current_scope.get("missing_target_issue_keys") or []
            ),
        },
    )


def build_jira_task_incremental_import_workflow_test_definition() -> WorkflowDefinition:
    run_sync = WorkflowStateSpec(
        state_id="run_sync",
        actions=(
            WorkflowActionInvocation(
                action_id=JIRA_TASK_INCREMENTAL_IMPORT_ACTION_ID,
                description=(
                    "Synchronise Jira tasks into Von using the shared migration runner."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                reason="sync_failed",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="sync_complete",
            ),
        ),
    )

    complete = WorkflowStateSpec(state_id="complete", terminal=True)
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
        initial_state="run_sync",
        states={
            "run_sync": run_sync,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Scheduled incremental Jira task import that reuses the canonical "
            "Jira discovery and task_import_jira_issues gateway path."
        ),
    )


def build_jira_task_incremental_import_workflow_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
        definition=build_jira_task_incremental_import_workflow_test_definition(),
        purpose=(
            "Background incremental Jira task import using the shared migration runner."
        ),
        source="built_in",
    )


def register_jira_task_incremental_import_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=JIRA_TASK_INCREMENTAL_IMPORT_ACTION_ID,
            handler=_handle_run_sync,
            description="Run the shared Jira task incremental import.",
            side_effects="write",
        )
    )


__all__ = [
    "JIRA_TASK_INCREMENTAL_IMPORT_ACTION_ID",
    "JIRA_TASK_INCREMENTAL_IMPORT_VERSION",
    "JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID",
    "build_jira_task_incremental_import_workflow_test_definition",
    "build_jira_task_incremental_import_workflow_test_registration",
    "register_jira_task_incremental_import_actions",
]
