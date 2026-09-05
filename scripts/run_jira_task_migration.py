from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_jira_task_migration_runner_service = importlib.import_module(
    "src.backend.services.jira_task_migration_runner_service"
)
DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY = (
    _jira_task_migration_runner_service.DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY
)
DEFAULT_JIRA_TASK_MIGRATION_REPORT_PATH = (
    _jira_task_migration_runner_service.DEFAULT_JIRA_TASK_MIGRATION_REPORT_PATH
)
JiraTaskMigrationOptions = _jira_task_migration_runner_service.JiraTaskMigrationOptions
run_jira_task_migration_sync = (
    _jira_task_migration_runner_service.run_jira_task_migration_sync
)


def _parse_args() -> JiraTaskMigrationOptions:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Jira -> Von task migration via paginated Jira discovery plus "
            "the canonical task_import_jira_issues gateway path. Supports both "
            "full current-scope reconciliation and recent-window incremental sync."
        )
    )
    parser.add_argument("--jql")
    parser.add_argument(
        "--project-key",
        default=DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY,
    )
    parser.add_argument("--actor-concept-id", required=True)
    parser.add_argument("--organisation-concept-id")
    parser.add_argument("--namespace")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--import-referenced-targets", action="store_true")
    parser.add_argument("--sync-source-labels", action="store_true")
    parser.add_argument("--source-migrated-label", default="migrated")
    parser.add_argument("--updated-within-hours", type=int)
    parser.add_argument("--include-done", action="store_true")
    parser.add_argument("--min-issue-number", type=int)
    parser.add_argument("--max-issue-number", type=int)
    parser.add_argument(
        "--report-path",
        default=str(DEFAULT_JIRA_TASK_MIGRATION_REPORT_PATH),
    )
    args = parser.parse_args()

    return JiraTaskMigrationOptions(
        actor_concept_id=str(args.actor_concept_id).strip(),
        organisation_concept_id=(
            str(args.organisation_concept_id).strip()
            if isinstance(args.organisation_concept_id, str)
            and args.organisation_concept_id.strip()
            else None
        ),
        namespace=(
            str(args.namespace).strip()
            if isinstance(args.namespace, str) and args.namespace.strip()
            else None
        ),
        project_key=str(args.project_key).strip().upper(),
        jql=(str(args.jql).strip() if isinstance(args.jql, str) and args.jql.strip() else None),
        batch_size=int(args.batch_size),
        page_size=int(args.page_size),
        passes=int(args.passes),
        dry_run=bool(args.dry_run),
        only_missing=bool(args.only_missing),
        import_referenced_targets=bool(args.import_referenced_targets),
        sync_source_labels=bool(args.sync_source_labels),
        source_migrated_label=str(args.source_migrated_label).strip() or "migrated",
        report_path=Path(args.report_path),
        updated_within_hours=(
            int(args.updated_within_hours)
            if isinstance(args.updated_within_hours, int)
            else None
        ),
        include_done=bool(args.include_done),
        min_issue_number=(
            int(args.min_issue_number)
            if isinstance(args.min_issue_number, int)
            else None
        ),
        max_issue_number=(
            int(args.max_issue_number)
            if isinstance(args.max_issue_number, int)
            else None
        ),
    )


def main() -> None:
    from src.backend.integrations.internal_mcp.gateway import (
        INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
        bind_internal_mcp_actor_context_source,
    )

    options = _parse_args()
    # This explicit local CLI is an operator entry point. Keep this authority
    # here, not in the runner also called by actor-bound durable workflows.
    with bind_internal_mcp_actor_context_source(
        INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE
    ):
        report = run_jira_task_migration_sync(options)
    print(json.dumps({"report_path": str(options.report_path), "summary": report}, indent=2))


if __name__ == "__main__":
    main()
