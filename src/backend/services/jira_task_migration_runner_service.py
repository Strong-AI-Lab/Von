"""Shared Jira task migration runner for bulk, incremental, and repair syncs.

This keeps every Jira->Von task import entry point on the same canonical path:
1. discover Jira issues via the Jira proxy,
2. reuse seeded issue documents where possible, and
3. invoke ``task_import_jira_issues`` through the internal MCP gateway.

The runner supports:
- full current-scope reconciliation,
- recent-window incremental sync using ``updated >= -Nh``,
- one-off recent catch-up by Jira issue-number range, and
- referenced-target repair passes for linked issues outside the main window.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence

from ..integrations.internal_mcp.jira_proxy_mcp import get_jira_proxy
from ..utils.jira_issue_key_utils import (
    extract_jira_issue_number,
    normalise_jira_issue_key,
)

if TYPE_CHECKING:
    from ..integrations.internal_mcp.gateway import InternalMCPGateway

DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY = "JVNAUTOSCI"
DEFAULT_JIRA_TASK_MIGRATION_BATCH_SIZE = 50
DEFAULT_JIRA_TASK_MIGRATION_PAGE_SIZE = 100
DEFAULT_JIRA_TASK_MIGRATION_PASSES = 2
DEFAULT_JIRA_TASK_MIGRATION_REPORT_PATH = Path(
    "artifacts/jira_task_migration_report.json"
)
DEFAULT_JIRA_TASK_MIGRATION_SOURCE_MIGRATED_LABEL = "migrated"
DEFAULT_JIRA_TASK_MIGRATION_FIELDS = [
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
    "issuetype",
    "created",
    "updated",
    "comment",
    "attachment",
    "worklog",
]


@dataclass(frozen=True)
class JiraTaskMigrationOptions:
    actor_concept_id: str
    organisation_concept_id: str | None = None
    namespace: str | None = None
    project_key: str = DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY
    jql: str | None = None
    batch_size: int = DEFAULT_JIRA_TASK_MIGRATION_BATCH_SIZE
    page_size: int = DEFAULT_JIRA_TASK_MIGRATION_PAGE_SIZE
    passes: int = DEFAULT_JIRA_TASK_MIGRATION_PASSES
    dry_run: bool = False
    only_missing: bool = False
    import_referenced_targets: bool = False
    sync_source_labels: bool = False
    source_migrated_label: str = DEFAULT_JIRA_TASK_MIGRATION_SOURCE_MIGRATED_LABEL
    mark_bulk_migration_collection: bool = True
    report_path: Path | None = DEFAULT_JIRA_TASK_MIGRATION_REPORT_PATH
    updated_within_hours: int | None = None
    include_done: bool = False
    preserve_source: bool = False
    resume: bool = False
    min_issue_number: int | None = None
    max_issue_number: int | None = None


def build_default_jira_task_migration_jql(
    *,
    project_key: str,
    include_done: bool,
    updated_within_hours: int | None,
) -> str:
    """Build the default project-scoped Jira discovery query."""

    project_key_clean = (
        str(project_key).strip().upper() or DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY
    )
    clauses = [
        f"project = {project_key_clean}",
    ]
    if not include_done:
        clauses.append("statusCategory != Done")
    if isinstance(updated_within_hours, int):
        clauses.append(f"updated >= -{max(1, updated_within_hours)}h")
        order_by = "updated ASC, key ASC"
    else:
        order_by = "key ASC"
    return " AND ".join(clauses) + f" ORDER BY {order_by}"


def normalise_jira_task_migration_options(
    options: JiraTaskMigrationOptions,
) -> JiraTaskMigrationOptions:
    """Return a bounded, internally consistent options dataclass."""

    actor_concept_id = str(options.actor_concept_id or "").strip()
    if not actor_concept_id:
        raise ValueError("actor_concept_id is required")

    project_key = (
        str(options.project_key or "").strip().upper()
        or DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY
    )
    batch_size = max(1, min(int(options.batch_size), 200))
    page_size = max(1, min(int(options.page_size), 200))
    passes = max(1, int(options.passes))

    updated_within_hours: int | None = None
    if isinstance(options.updated_within_hours, int):
        updated_within_hours = max(1, int(options.updated_within_hours))

    min_issue_number: int | None = None
    if isinstance(options.min_issue_number, int):
        min_issue_number = max(1, int(options.min_issue_number))

    max_issue_number: int | None = None
    if isinstance(options.max_issue_number, int):
        max_issue_number = max(1, int(options.max_issue_number))

    if (
        isinstance(min_issue_number, int)
        and isinstance(max_issue_number, int)
        and min_issue_number > max_issue_number
    ):
        raise ValueError("min_issue_number must be <= max_issue_number")

    jql = str(options.jql or "").strip() or None
    if jql is None:
        jql = build_default_jira_task_migration_jql(
            project_key=project_key,
            include_done=bool(options.include_done),
            updated_within_hours=updated_within_hours,
        )

    organisation_concept_id = (
        str(options.organisation_concept_id).strip()
        if isinstance(options.organisation_concept_id, str)
        and options.organisation_concept_id.strip()
        else None
    )
    namespace = (
        str(options.namespace).strip()
        if isinstance(options.namespace, str) and options.namespace.strip()
        else None
    )
    source_migrated_label = (
        str(options.source_migrated_label or "").strip()
        or DEFAULT_JIRA_TASK_MIGRATION_SOURCE_MIGRATED_LABEL
    )
    report_path = None
    if options.report_path is not None:
        report_path = Path(options.report_path)

    return JiraTaskMigrationOptions(
        actor_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
        project_key=project_key,
        jql=jql,
        batch_size=batch_size,
        page_size=page_size,
        passes=passes,
        dry_run=bool(options.dry_run),
        only_missing=bool(options.only_missing),
        import_referenced_targets=bool(options.import_referenced_targets),
        sync_source_labels=bool(options.sync_source_labels),
        source_migrated_label=source_migrated_label,
        mark_bulk_migration_collection=bool(options.mark_bulk_migration_collection),
        report_path=report_path,
        updated_within_hours=updated_within_hours,
        include_done=bool(options.include_done),
        min_issue_number=min_issue_number,
        max_issue_number=max_issue_number,
        preserve_source=bool(options.preserve_source),
        resume=bool(options.resume),
    )


def _iter_batches(
    items: Sequence[Mapping[str, Any]],
    batch_size: int,
) -> Iterator[list[Mapping[str, Any]]]:
    for start in range(0, len(items), batch_size):
        yield list(items[start : start + batch_size])


async def discover_jira_issue_docs(
    *,
    jql: str,
    fields: Sequence[str],
    page_size: int,
) -> list[dict[str, Any]]:
    proxy = await get_jira_proxy()
    next_page_token: str | None = None
    issue_docs: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()

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
        if not isinstance(raw_issues, list) or payload.get("success") is False:
            raise RuntimeError(
                "Jira discovery failed; an error is not an empty project"
            )
        if not raw_issues and next_page_token:
            raise RuntimeError("Jira discovery returned an empty non-terminal page")
        issue_docs.extend(item for item in raw_issues if isinstance(item, dict))
        if not isinstance(next_page_token, str) or not next_page_token.strip():
            if payload.get("isLast") is False:
                raise RuntimeError("Jira discovery omitted the next page token")
            break
        if next_page_token in seen_tokens:
            raise RuntimeError("Jira discovery pagination stalled")
        seen_tokens.add(next_page_token)

    return issue_docs


async def _fetch_issue_docs_by_key(
    issue_keys: Sequence[str],
    *,
    fields: Sequence[str],
) -> list[dict[str, Any]]:
    proxy = await get_jira_proxy()
    issue_docs: list[dict[str, Any]] = []
    for issue_key in issue_keys:
        payload = await proxy.get_issue(issue_key=issue_key, fields=list(fields))
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
    project_prefix = f"{project_key.strip().upper()}-"
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
                issue_key = normalise_jira_issue_key(
                    relation_row.get("target_issue_key")
                )
                if isinstance(issue_key, str) and issue_key.startswith(project_prefix):
                    missing_targets.add(issue_key)
    return sorted(missing_targets)


def _build_gateway() -> InternalMCPGateway:
    from ..integrations.internal_mcp import build_default_catalogue
    from ..integrations.internal_mcp.gateway import (
        InternalMCPGateway,
        internal_mcp_actor_context_is_trusted_local_operator,
    )
    from ..integrations.internal_mcp.transport import InternalMCPTransport

    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=internal_mcp_actor_context_is_trusted_local_operator(),
    )


def _run_import_batches(
    *,
    gateway: InternalMCPGateway,
    issue_docs: Sequence[Mapping[str, Any]],
    options: JiraTaskMigrationOptions,
    run_label: str,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    aggregate_summary: Counter[str] = Counter()
    checkpoint = (
        options.report_path.with_suffix(f".{run_label}.checkpoint.json")
        if options.report_path is not None
        else None
    )
    signature_payload = {
        "options": {
            key: value
            for key, value in _options_to_report_dict(options).items()
            if key not in {"resume", "report_path"}
        },
        "source_versions": [
            (item.get("id"), item.get("key"), item.get("fields", {}).get("updated"))
            for item in issue_docs
        ],
        "run_label": run_label,
    }
    signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True).encode()
    ).hexdigest()
    if options.resume and checkpoint and checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if saved.get("signature") != signature:
            raise ValueError(
                "Migration checkpoint scope/source changed; start a fresh run for the new delta"
            )
        reports = saved.get("completed_batches", [])
        for report in reports:
            _merge_counter(aggregate_summary, report.get("summary"))
    completed_count = len(reports)
    batch_index = 0

    # The historical extra pass only repaired cross-batch relationships. Full
    # source capture is followed by that same repair using the retained rows.
    capture_passes = 1 if options.preserve_source else options.passes
    for pass_index in range(capture_passes):
        for batch in _iter_batches(issue_docs, options.batch_size):
            batch_index += 1
            if batch_index <= completed_count:
                continue
            payload = {
                "issue_keys": [
                    dict(item) for item in batch if isinstance(item, Mapping)
                ],
                "dry_run": options.dry_run,
                "preserve_source": options.preserve_source,
                "update_existing": True,
                "include_watchers": False,
                "namespace": options.namespace,
                "actor_concept_id": options.actor_concept_id,
                "organisation_concept_id": options.organisation_concept_id,
                "auto_resolve_participants": True,
                "create_missing_participant_concepts": True,
                "sync_source_labels": options.sync_source_labels,
                "source_migrated_label": options.source_migrated_label,
                "mark_bulk_migration_collection": bool(
                    options.mark_bulk_migration_collection
                    and options.updated_within_hours is None
                ),
            }
            result = gateway.invoke("task_import_jira_issues", payload).payload
            succeeded = isinstance(result, dict) and result.get("success") is True
            if succeeded:
                reports.append(result)
                _merge_counter(aggregate_summary, result.get("summary"))
            if checkpoint is not None:
                write_jira_task_migration_report(
                    {
                        "signature": signature,
                        "run_label": run_label,
                        "pass_index": pass_index,
                        "completed_batches": reports,
                        "last_batch": result,
                    },
                    report_path=checkpoint,
                )
            if not succeeded:
                raise RuntimeError(
                    f"{run_label} pass {pass_index + 1} failed: {result}"
                )

    relation_repair = None
    if options.preserve_source and reports:
        from .jira_task_import_service import repair_jira_task_relations

        rows = [row for report in reports for row in report.get("issues", [])]
        relation_repair = repair_jira_task_relations(
            rows,
            actor_concept_id=options.actor_concept_id,
            organisation_concept_id=options.organisation_concept_id,
            dry_run=options.dry_run,
            only_unresolved=True,
        )
        relations = [
            relation for row in rows for relation in row.get("relation_results", [])
        ]
        aggregate_summary["mapped_relations"] = sum(
            row.get("status") in {"mapped", "would_map"} for row in relations
        )
        aggregate_summary["dropped_relations"] = sum(
            row.get("status") == "dropped" for row in relations
        )
    return {
        "run_label": run_label,
        "passes": capture_passes,
        "relation_repair": relation_repair,
        "batch_size": options.batch_size,
        "input_issue_count": len(issue_docs),
        "batch_count": len(reports),
        "summary": dict(aggregate_summary),
        "reports": reports,
        "missing_target_issue_keys": _extract_missing_targets(
            reports,
            project_key=options.project_key,
        ),
    }


def _filter_issue_docs_by_issue_number(
    issue_docs: Sequence[Mapping[str, Any]],
    *,
    min_issue_number: int | None,
    max_issue_number: int | None,
) -> tuple[list[Mapping[str, Any]], int]:
    if min_issue_number is None and max_issue_number is None:
        return list(issue_docs), 0

    selected: list[Mapping[str, Any]] = []
    filtered_out_count = 0
    for issue_doc in issue_docs:
        issue_number = extract_jira_issue_number(issue_doc.get("key"))
        if issue_number is None:
            filtered_out_count += 1
            continue
        if isinstance(min_issue_number, int) and issue_number < min_issue_number:
            filtered_out_count += 1
            continue
        if isinstance(max_issue_number, int) and issue_number > max_issue_number:
            filtered_out_count += 1
            continue
        selected.append(issue_doc)
    return selected, filtered_out_count


def _options_to_report_dict(
    options: JiraTaskMigrationOptions,
) -> dict[str, Any]:
    return {
        "project_key": options.project_key,
        "jql": options.jql,
        "actor_concept_id": options.actor_concept_id,
        "organisation_concept_id": options.organisation_concept_id,
        "namespace": options.namespace,
        "batch_size": options.batch_size,
        "page_size": options.page_size,
        "passes": options.passes,
        "dry_run": options.dry_run,
        "only_missing": options.only_missing,
        "import_referenced_targets": options.import_referenced_targets,
        "sync_source_labels": options.sync_source_labels,
        "source_migrated_label": options.source_migrated_label,
        "mark_bulk_migration_collection": options.mark_bulk_migration_collection,
        "report_path": str(options.report_path) if options.report_path else None,
        "updated_within_hours": options.updated_within_hours,
        "include_done": options.include_done,
        "preserve_source": options.preserve_source,
        "resume": options.resume,
        "min_issue_number": options.min_issue_number,
        "max_issue_number": options.max_issue_number,
    }


def write_jira_task_migration_report(
    report: Mapping[str, Any],
    *,
    report_path: Path,
) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(report_path)
    return report_path


async def run_jira_task_migration(
    options: JiraTaskMigrationOptions,
    *,
    gateway: InternalMCPGateway | None = None,
) -> dict[str, Any]:
    """Run the Jira task migration and return a deterministic summary report."""

    options = normalise_jira_task_migration_options(options)
    gateway = gateway or _build_gateway()

    discovered_issue_docs = await discover_jira_issue_docs(
        jql=str(options.jql),
        fields=DEFAULT_JIRA_TASK_MIGRATION_FIELDS,
        page_size=options.page_size,
    )
    discovered_by_key = {
        issue_key: issue_doc
        for issue_doc in discovered_issue_docs
        if isinstance(issue_doc, Mapping)
        for issue_key in [normalise_jira_issue_key(issue_doc.get("key"))]
        if isinstance(issue_key, str)
    }
    deduplicated_issue_docs = list(discovered_by_key.values())

    candidate_issue_docs, issue_number_filtered_out_count = (
        _filter_issue_docs_by_issue_number(
            deduplicated_issue_docs,
            min_issue_number=options.min_issue_number,
            max_issue_number=options.max_issue_number,
        )
    )

    from .jira_task_import_service import list_imported_jira_issue_keys

    imported_issue_keys = set(
        list_imported_jira_issue_keys(
            organisation_concept_id=options.organisation_concept_id,
            limit=None,
        )
    )
    already_imported_selected_issue_count = sum(
        1
        for issue_doc in candidate_issue_docs
        if normalise_jira_issue_key(issue_doc.get("key")) in imported_issue_keys
    )

    selected_issue_docs = list(candidate_issue_docs)
    if options.only_missing:
        selected_issue_docs = [
            issue_doc
            for issue_doc in candidate_issue_docs
            if normalise_jira_issue_key(issue_doc.get("key")) not in imported_issue_keys
        ]

    selection_mode = "explicit_jql"
    if (
        not options.include_done
        and options.updated_within_hours is None
        and not options.only_missing
    ):
        selection_mode = "current_scope"
    if options.updated_within_hours is not None:
        selection_mode = "incremental_recent_window"

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "options": _options_to_report_dict(options),
        "discovery": {
            "selection_mode": selection_mode,
            "discovered_issue_count": len(discovered_issue_docs),
            "deduplicated_issue_count": len(deduplicated_issue_docs),
            "duplicate_issue_count": len(discovered_issue_docs)
            - len(deduplicated_issue_docs),
            "issue_number_filtered_out_count": issue_number_filtered_out_count,
            "candidate_selected_issue_count": len(candidate_issue_docs),
            "already_imported_issue_count": len(imported_issue_keys),
            "already_imported_selected_issue_count": already_imported_selected_issue_count,
            "selected_issue_count": len(selected_issue_docs),
            "selected_issue_keys_sample": [
                issue_key
                for issue_doc in selected_issue_docs[:20]
                for issue_key in [normalise_jira_issue_key(issue_doc.get("key"))]
                if isinstance(issue_key, str)
            ],
        },
    }

    main_run = _run_import_batches(
        gateway=gateway,
        issue_docs=selected_issue_docs,
        options=options,
        run_label="current_scope",
    )
    report["current_scope"] = {key: value for key, value in main_run.items()}

    referenced_target_keys = main_run.get("missing_target_issue_keys") or []
    if options.import_referenced_targets and referenced_target_keys:
        target_issue_docs = await _fetch_issue_docs_by_key(
            referenced_target_keys,
            fields=DEFAULT_JIRA_TASK_MIGRATION_FIELDS,
        )
        referenced_run = _run_import_batches(
            gateway=gateway,
            issue_docs=target_issue_docs,
            options=options,
            run_label="referenced_targets",
        )
        report["referenced_targets"] = {
            key: value for key, value in referenced_run.items() if key != "reports"
        }

        # Repair current-scope links after imported targets become available.
        repair_run = _run_import_batches(
            gateway=gateway,
            issue_docs=selected_issue_docs,
            options=replace(options, passes=1),
            run_label="current_scope_repair",
        )
        report["current_scope_repair"] = {
            key: value for key, value in repair_run.items() if key != "reports"
        }

    if options.report_path is not None:
        write_jira_task_migration_report(report, report_path=options.report_path)

    return report


def run_jira_task_migration_sync(
    options: JiraTaskMigrationOptions,
    *,
    gateway: InternalMCPGateway | None = None,
) -> dict[str, Any]:
    """Synchronous wrapper for CLI and durable-workflow callers."""

    return asyncio.run(run_jira_task_migration(options, gateway=gateway))


__all__ = [
    "DEFAULT_JIRA_TASK_MIGRATION_FIELDS",
    "DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY",
    "DEFAULT_JIRA_TASK_MIGRATION_REPORT_PATH",
    "JiraTaskMigrationOptions",
    "build_default_jira_task_migration_jql",
    "discover_jira_issue_docs",
    "normalise_jira_task_migration_options",
    "run_jira_task_migration",
    "run_jira_task_migration_sync",
    "write_jira_task_migration_report",
]
