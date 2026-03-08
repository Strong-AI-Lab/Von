from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Iterator, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.jira_proxy_mcp import get_jira_proxy
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.services.jira_task_import_service import list_imported_jira_issue_keys

DEFAULT_JQL = (
    "project = JVNAUTOSCI AND issuetype in (Task, Subtask, Epic) "
    "AND statusCategory != Done ORDER BY key ASC"
)
DEFAULT_FIELDS = [
    "summary",
    "description",
    "status",
    "priority",
    "labels",
    "project",
    "duedate",
    "startdate",
    "assignee",
    "creator",
    "reporter",
    "parent",
    "issuelinks",
    "components",
    "fixVersions",
    "customfield_10020",
    "customfield_10019",
    "customfield_10027",
    "customfield_10014",
    "customfield_10008",
]


@dataclass(frozen=True)
class MigrationOptions:
    jql: str
    project_key: str
    actor_concept_id: str
    organisation_concept_id: str | None
    namespace: str | None
    batch_size: int
    passes: int
    dry_run: bool
    only_missing: bool
    import_referenced_targets: bool
    sync_source_labels: bool
    source_migrated_label: str
    report_path: Path


def _parse_args() -> MigrationOptions:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Jira -> Von task migration via paginated Jira discovery plus "
            "the canonical task_import_jira_issues gateway path."
        )
    )
    parser.add_argument("--jql", default=DEFAULT_JQL)
    parser.add_argument("--project-key", default="JVNAUTOSCI")
    parser.add_argument("--actor-concept-id", required=True)
    parser.add_argument("--organisation-concept-id")
    parser.add_argument("--namespace")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--import-referenced-targets", action="store_true")
    parser.add_argument("--sync-source-labels", action="store_true")
    parser.add_argument("--source-migrated-label", default="migrated")
    parser.add_argument(
        "--report-path",
        default="artifacts/jira_task_migration_report.json",
    )
    args = parser.parse_args()

    batch_size = max(1, min(int(args.batch_size), 200))
    passes = max(1, int(args.passes))
    project_key = str(args.project_key).strip().upper()
    return MigrationOptions(
        jql=str(args.jql).strip(),
        project_key=project_key,
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
        batch_size=batch_size,
        passes=passes,
        dry_run=bool(args.dry_run),
        only_missing=bool(args.only_missing),
        import_referenced_targets=bool(args.import_referenced_targets),
        sync_source_labels=bool(args.sync_source_labels),
        source_migrated_label=str(args.source_migrated_label).strip() or "migrated",
        report_path=Path(args.report_path),
    )


def _iter_batches(items: Sequence[Mapping[str, Any]], batch_size: int) -> Iterator[list[Mapping[str, Any]]]:
    for start in range(0, len(items), batch_size):
        yield list(items[start : start + batch_size])


def _normalise_issue_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    return cleaned or None


async def _discover_issue_docs(
    *,
    jql: str,
    fields: Sequence[str],
    page_size: int,
) -> list[dict[str, Any]]:
    proxy = await get_jira_proxy()
    next_page_token: str | None = None
    issue_docs: list[dict[str, Any]] = []

    while True:
        payload = await proxy.search(
            jql=jql,
            max_results=page_size,
            next_page_token=next_page_token,
            fields=list(fields),
        )
        raw_issues = payload.get("issues") if isinstance(payload, Mapping) else None
        next_page_token = (
            payload.get("nextPageToken") if isinstance(payload, Mapping) else None
        )
        if not isinstance(raw_issues, list) or not raw_issues:
            break
        issue_docs.extend(item for item in raw_issues if isinstance(item, dict))
        if not isinstance(next_page_token, str) or not next_page_token.strip():
            break

    return issue_docs


async def _fetch_issue_docs_by_key(issue_keys: Sequence[str]) -> list[dict[str, Any]]:
    proxy = await get_jira_proxy()
    issue_docs: list[dict[str, Any]] = []
    for issue_key in issue_keys:
        payload = await proxy.get_issue(issue_key=issue_key)
        if isinstance(payload, dict):
            issue_docs.append(payload)
    return issue_docs


def _merge_counter(target: Counter[str], source: Mapping[str, Any] | None) -> None:
    if not isinstance(source, Mapping):
        return
    for key, value in source.items():
        if isinstance(value, bool):
            target[str(key)] += int(value)
            continue
        if isinstance(value, int):
            target[str(key)] += value


def _extract_missing_targets(
    reports: Sequence[Mapping[str, Any]],
    *,
    project_key: str,
) -> list[str]:
    missing_targets: set[str] = set()
    for report in reports:
        issue_rows = report.get("issues")
        if not isinstance(issue_rows, list):
            continue
        for issue_row in issue_rows:
            if not isinstance(issue_row, Mapping):
                continue
            relation_rows = issue_row.get("relation_results")
            if not isinstance(relation_rows, list):
                continue
            for relation_row in relation_rows:
                if not isinstance(relation_row, Mapping):
                    continue
                if relation_row.get("reason") != "target_issue_not_imported":
                    continue
                issue_key = _normalise_issue_key(relation_row.get("target_issue_key"))
                if (
                    isinstance(issue_key, str)
                    and issue_key.startswith(f"{project_key}-")
                ):
                    missing_targets.add(issue_key)
    return sorted(missing_targets)


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _run_import_batches(
    *,
    gateway: InternalMCPGateway,
    issue_docs: Sequence[Mapping[str, Any]],
    options: MigrationOptions,
    run_label: str,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    aggregate_summary: Counter[str] = Counter()

    for pass_index in range(options.passes):
        for batch_index, batch in enumerate(_iter_batches(issue_docs, options.batch_size), start=1):
            payload = {
                "issue_keys": [dict(item) for item in batch if isinstance(item, Mapping)],
                "dry_run": options.dry_run,
                "update_existing": True,
                "include_watchers": False,
                "namespace": options.namespace,
                "actor_concept_id": options.actor_concept_id,
                "organisation_concept_id": options.organisation_concept_id,
                "auto_resolve_participants": True,
                "create_missing_participant_concepts": True,
                "sync_source_labels": options.sync_source_labels,
                "source_migrated_label": options.source_migrated_label,
            }
            result = gateway.invoke("task_import_jira_issues", payload).payload
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RuntimeError(
                    f"{run_label} pass {pass_index + 1} batch {batch_index} failed: {result}"
                )
            reports.append(result)
            _merge_counter(aggregate_summary, result.get("summary"))
            print(
                json.dumps(
                    {
                        "run": run_label,
                        "pass": pass_index + 1,
                        "batch": batch_index,
                        "batch_issue_count": len(batch),
                        "summary": result.get("summary"),
                    }
                )
            )

    return {
        "run_label": run_label,
        "passes": options.passes,
        "batch_size": options.batch_size,
        "batch_count": len(reports),
        "summary": dict(aggregate_summary),
        "reports": reports,
        "missing_target_issue_keys": _extract_missing_targets(
            reports,
            project_key=options.project_key,
        ),
    }


async def _run(options: MigrationOptions) -> dict[str, Any]:
    gateway = _build_gateway()

    discovered_issue_docs = await _discover_issue_docs(
        jql=options.jql,
        fields=DEFAULT_FIELDS,
        page_size=100,
    )
    discovered_by_key = {
        issue_key: issue_doc
        for issue_doc in discovered_issue_docs
        if isinstance(issue_doc, Mapping)
        for issue_key in [_normalise_issue_key(issue_doc.get("key"))]
        if isinstance(issue_key, str)
    }
    imported_issue_keys = set(
        list_imported_jira_issue_keys(
            organisation_concept_id=options.organisation_concept_id,
            limit=2000,
        )
    )

    selected_issue_docs = list(discovered_by_key.values())
    if options.only_missing:
        selected_issue_docs = [
            issue_doc
            for issue_doc in selected_issue_docs
            if (
                issue_key := _normalise_issue_key(issue_doc.get("key"))
            ) not in imported_issue_keys
        ]

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "options": {
            "jql": options.jql,
            "project_key": options.project_key,
            "actor_concept_id": options.actor_concept_id,
            "organisation_concept_id": options.organisation_concept_id,
            "namespace": options.namespace,
            "batch_size": options.batch_size,
            "passes": options.passes,
            "dry_run": options.dry_run,
            "only_missing": options.only_missing,
            "import_referenced_targets": options.import_referenced_targets,
            "sync_source_labels": options.sync_source_labels,
            "source_migrated_label": options.source_migrated_label,
        },
        "discovery": {
            "discovered_issue_count": len(discovered_issue_docs),
            "selected_issue_count": len(selected_issue_docs),
            "already_imported_issue_count": len(imported_issue_keys),
        },
    }

    main_run = _run_import_batches(
        gateway=gateway,
        issue_docs=selected_issue_docs,
        options=options,
        run_label="current_scope",
    )
    report["current_scope"] = {
        key: value for key, value in main_run.items() if key != "reports"
    }

    referenced_target_keys = main_run.get("missing_target_issue_keys") or []
    if options.import_referenced_targets and referenced_target_keys:
        target_issue_docs = await _fetch_issue_docs_by_key(referenced_target_keys)
        referenced_run = _run_import_batches(
            gateway=gateway,
            issue_docs=target_issue_docs,
            options=options,
            run_label="referenced_targets",
        )
        report["referenced_targets"] = {
            key: value for key, value in referenced_run.items() if key != "reports"
        }

        # A final current-scope pass repairs links whose targets only became
        # available after the referenced-target import completed.
        repair_run = _run_import_batches(
            gateway=gateway,
            issue_docs=selected_issue_docs,
            options=MigrationOptions(
                jql=options.jql,
                project_key=options.project_key,
                actor_concept_id=options.actor_concept_id,
                organisation_concept_id=options.organisation_concept_id,
                namespace=options.namespace,
                batch_size=options.batch_size,
                passes=1,
                dry_run=options.dry_run,
                only_missing=options.only_missing,
                import_referenced_targets=options.import_referenced_targets,
                sync_source_labels=options.sync_source_labels,
                source_migrated_label=options.source_migrated_label,
                report_path=options.report_path,
            ),
            run_label="current_scope_repair",
        )
        report["current_scope_repair"] = {
            key: value for key, value in repair_run.items() if key != "reports"
        }

    return report


def main() -> None:
    options = _parse_args()
    report = asyncio.run(_run(options))
    options.report_path.parent.mkdir(parents=True, exist_ok=True)
    options.report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report_path": str(options.report_path), "summary": report}, indent=2))


if __name__ == "__main__":
    main()
