"""Shared Jira task reconciliation support for workflow-native backfills.

The full reconciliation workflow needs one reusable primitive that VWL cannot
yet express directly: compare the actual Jira task set with already imported
Von task representations, then choose the next bounded contiguous missing
issue-number block to import.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping

from ..utils.jira_issue_key_utils import (
    extract_jira_issue_number,
    normalise_jira_issue_key,
)
from .jira_task_migration_runner_service import (
    DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY,
    build_default_jira_task_migration_jql,
    discover_jira_issue_docs,
)

_DEFAULT_PAGE_SIZE = 100
_DEFAULT_GAP_BLOCK_SIZE = 100
_MAX_PAGE_SIZE = 200
_MAX_GAP_BLOCK_SIZE = 500


@dataclass(frozen=True)
class JiraTaskGapScanOptions:
    project_key: str = DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY
    organisation_concept_id: str | None = None
    jql: str | None = None
    page_size: int = _DEFAULT_PAGE_SIZE
    gap_block_size: int = _DEFAULT_GAP_BLOCK_SIZE
    include_done: bool = True


def normalise_jira_task_gap_scan_options(
    options: JiraTaskGapScanOptions,
) -> JiraTaskGapScanOptions:
    """Return bounded options for deterministic gap scanning."""

    project_key = (
        str(options.project_key or "").strip().upper()
        or DEFAULT_JIRA_TASK_MIGRATION_PROJECT_KEY
    )
    organisation_concept_id = (
        str(options.organisation_concept_id).strip()
        if isinstance(options.organisation_concept_id, str)
        and options.organisation_concept_id.strip()
        else None
    )
    jql = str(options.jql or "").strip() or None
    page_size = max(1, min(int(options.page_size), _MAX_PAGE_SIZE))
    gap_block_size = max(1, min(int(options.gap_block_size), _MAX_GAP_BLOCK_SIZE))
    include_done = bool(options.include_done)

    if jql is None:
        jql = build_default_jira_task_migration_jql(
            project_key=project_key,
            include_done=include_done,
            updated_within_hours=None,
        )

    return JiraTaskGapScanOptions(
        project_key=project_key,
        organisation_concept_id=organisation_concept_id,
        jql=jql,
        page_size=page_size,
        gap_block_size=gap_block_size,
        include_done=include_done,
    )


def _issue_number_blocks(numbers: list[int]) -> list[tuple[int, int]]:
    if not numbers:
        return []

    blocks: list[tuple[int, int]] = []
    block_start = numbers[0]
    previous = numbers[0]

    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        blocks.append((block_start, previous))
        block_start = number
        previous = number

    blocks.append((block_start, previous))
    return blocks


async def scan_next_jira_task_gap_block(
    options: JiraTaskGapScanOptions,
) -> dict[str, Any]:
    """Compare Jira task keys against imported Von tasks and pick the next gap."""

    from .jira_task_import_service import list_imported_jira_issue_keys

    options = normalise_jira_task_gap_scan_options(options)
    project_prefix = f"{options.project_key}-"

    discovered_issue_docs = await discover_jira_issue_docs(
        jql=str(options.jql),
        fields=["summary"],
        page_size=options.page_size,
    )
    discovered_by_key = {
        issue_key: dict(issue_doc)
        for issue_doc in discovered_issue_docs
        if isinstance(issue_doc, Mapping)
        for issue_key in [normalise_jira_issue_key(issue_doc.get("key"))]
        if isinstance(issue_key, str) and issue_key.startswith(project_prefix)
    }

    actual_issue_key_by_number = {
        issue_number: issue_key
        for issue_key in sorted(discovered_by_key)
        for issue_number in [extract_jira_issue_number(issue_key)]
        if isinstance(issue_number, int)
    }
    actual_issue_numbers = sorted(actual_issue_key_by_number)

    imported_issue_keys = {
        issue_key
        for issue_key in list_imported_jira_issue_keys(
            organisation_concept_id=options.organisation_concept_id,
            limit=None,
        )
        if issue_key.startswith(project_prefix)
    }

    missing_issue_numbers = [
        issue_number
        for issue_number, issue_key in actual_issue_key_by_number.items()
        if issue_key not in imported_issue_keys
    ]
    missing_issue_numbers.sort()
    missing_blocks = _issue_number_blocks(missing_issue_numbers)

    gap_block_found = bool(missing_blocks)
    selected_gap_block: dict[str, Any] | None = None
    remaining_block_count_after_selected_block = 0

    if gap_block_found:
        block_start, block_end = missing_blocks[0]
        selected_end = min(block_end, block_start + options.gap_block_size - 1)
        selected_issue_numbers = list(range(block_start, selected_end + 1))
        selected_issue_keys = [
            actual_issue_key_by_number[number]
            for number in selected_issue_numbers
            if number in actual_issue_key_by_number
        ]
        remaining_block_count_after_selected_block = max(0, len(missing_blocks) - 1)
        if selected_end < block_end:
            remaining_block_count_after_selected_block += 1
        selected_gap_block = {
            "min_issue_number": block_start,
            "max_issue_number": selected_end,
            "selected_from_block_max_issue_number": block_end,
            "issue_count": len(selected_issue_keys),
            "issue_keys": selected_issue_keys,
        }

    highest_observed_issue_number = (
        actual_issue_numbers[-1] if actual_issue_numbers else None
    )
    selected_issue_count = (
        int(selected_gap_block.get("issue_count") or 0)
        if isinstance(selected_gap_block, Mapping)
        else 0
    )

    return {
        "project_key": options.project_key,
        "jql": options.jql,
        "include_done": options.include_done,
        "page_size": options.page_size,
        "gap_block_size": options.gap_block_size,
        "discovered_issue_count": len(discovered_issue_docs),
        "deduplicated_project_issue_count": len(discovered_by_key),
        "highest_observed_issue_number": highest_observed_issue_number,
        "imported_project_issue_count": len(imported_issue_keys),
        "missing_issue_count": len(missing_issue_numbers),
        "missing_issue_keys_sample": [
            actual_issue_key_by_number[number] for number in missing_issue_numbers[:20]
        ],
        "missing_block_count": len(missing_blocks),
        "gap_block_found": gap_block_found,
        "selected_gap_block": selected_gap_block,
        "remaining_block_count_after_selected_block": (
            remaining_block_count_after_selected_block
        ),
        "remaining_missing_issue_count_after_selected_block": max(
            0,
            len(missing_issue_numbers) - selected_issue_count,
        ),
    }


def scan_next_jira_task_gap_block_sync(
    options: JiraTaskGapScanOptions,
) -> dict[str, Any]:
    """Synchronous wrapper for durable workflow actions."""

    return asyncio.run(scan_next_jira_task_gap_block(options))


__all__ = [
    "JiraTaskGapScanOptions",
    "normalise_jira_task_gap_scan_options",
    "scan_next_jira_task_gap_block",
    "scan_next_jira_task_gap_block_sync",
]
