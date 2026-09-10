"""Shared Jira task reconciliation support for workflow-native backfills.

The full reconciliation workflow needs one reusable primitive that VWL cannot
yet express directly: compare the actual Jira task set with already imported
Von task representations, then choose the next bounded contiguous missing
issue-number block to import.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping
from collections import Counter, defaultdict
from datetime import datetime, timezone

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


def reconcile_retained_project(
    *, source_issues, project_concept_id, actor_concept_id, verify_archives=True
):
    """Reconcile identities, versions, memberships and retained bytes explicitly.

    Reads canonical repositories and archive services. Missing source rows never
    authorise deleting a task or archive; they remain inspectable discrepancies.
    This is retention evidence, not a substitute for ordinary-use/restore proof.
    """
    from ..db.repositories.concepts_repository import ConceptsRepository
    from .task_project_service import get_task_project, get_task_collection
    from .jira_source_retention_service import read_source_archive
    from .task_management_service import (
        PREDICATE_HAS_PARENT_TASK,
        PREDICATE_HAS_EPIC_TASK,
        TASK_LINK_TYPE_TO_PREDICATE,
    )
    from .jira_task_import_service import (
        _extract_parent_issue_key,
        _extract_epic_issue_key,
        _extract_issue_links,
    )

    project = get_task_project(project_concept_id)
    project_key = project["key"]
    source_by_key, source_ids = {}, Counter()
    problems, verified, hashes = [], [], []
    for issue in source_issues:
        key = issue.get("key")
        if (
            not key
            or issue.get("fields", {}).get("project", {}).get("key") != project_key
        ):
            problems.append({"kind": "source_scope_mismatch", "key": key})
            continue
        if key in source_by_key:
            problems.append({"kind": "duplicate_source_key", "key": key})
        source_by_key[key] = issue
        source_ids[str(issue.get("id"))] += 1
    problems.extend(
        {"kind": "duplicate_source_id", "id": key}
        for key, count in source_ids.items()
        if count != 1
    )
    docs = list(
        ConceptsRepository.find(
            {
                "relationships.is_an_instance_of": "#V#task_specification",
                "$or": [
                    {"metadata.external_references.jira.project_key": project_key},
                    {"metadata.project_concept_id": project_concept_id},
                ],
            },
            projection={"concept_id": 1, "metadata": 1, "relationships": 1},
        )
    )
    by_key = defaultdict(list)
    for doc in docs:
        ref = doc.get("metadata", {}).get("external_references", {}).get("jira", {})
        if ref.get("external_id"):
            by_key[ref["external_id"]].append(doc)
    missing, extra = sorted(set(source_by_key) - set(by_key)), sorted(
        set(by_key) - set(source_by_key)
    )
    problems.extend({"kind": "missing_task", "key": key} for key in missing)
    problems.extend({"kind": "source_absent_or_moved", "key": key} for key in extra)
    collections = [
        get_task_collection(value) for value in project["collection_concept_ids"]
    ]
    required_collections = {
        c["concept_id"]
        for c in collections
        if c["selection"]["kind"] == "project_membership"
    }
    for collection in collections:
        if project_concept_id not in collection["project_concept_ids"]:
            problems.append(
                {
                    "kind": "collection_project_mismatch",
                    "collection": collection["concept_id"],
                }
            )
    source_relations = {}
    target_keys = set()
    for key, issue in source_by_key.items():
        fields = issue.get("fields", {})
        relations = []
        for kind, target in (
            ("parent", _extract_parent_issue_key(fields)),
            ("epic", _extract_epic_issue_key(fields)),
        ):
            if target:
                relations.append({"kind": kind, "target_issue_key": target})
        relations.extend(
            {"kind": "link", **value} for value in _extract_issue_links(fields)
        )
        source_relations[key] = relations
        target_keys.update(row["target_issue_key"] for row in relations)
    external_targets = target_keys - set(by_key)
    target_by_key = dict(by_key)
    if external_targets:
        for doc in ConceptsRepository.find(
            {
                "relationships.is_an_instance_of": "#V#task_specification",
                "metadata.external_references.jira.external_id": {
                    "$in": sorted(external_targets)
                },
            },
            projection={
                "concept_id": 1,
                "metadata.external_references.jira": 1,
                "relationships": 1,
            },
        ):
            key = (
                doc.get("metadata", {})
                .get("external_references", {})
                .get("jira", {})
                .get("external_id")
            )
            target_by_key.setdefault(key, []).append(doc)
    for key, issue in source_by_key.items():
        candidates = by_key.get(key, [])
        if len(candidates) != 1:
            if candidates:
                problems.append(
                    {
                        "kind": "duplicate_task",
                        "key": key,
                        "task_ids": [x["concept_id"] for x in candidates],
                    }
                )
            continue
        doc = candidates[0]
        meta = doc.get("metadata", {})
        ref = meta.get("external_references", {}).get("jira", {})
        if str(ref.get("issue_id")) != str(issue.get("id")):
            problems.append({"kind": "source_id_mismatch", "key": key})
        if ref.get("updated") != issue.get("fields", {}).get("updated"):
            problems.append({"kind": "source_version_mismatch", "key": key})
        if meta.get(
            "project_concept_id"
        ) != project_concept_id or not required_collections.issubset(
            meta.get("collection_concept_ids") or []
        ):
            problems.append({"kind": "task_membership_mismatch", "key": key})
        if ref.get("sync_conflicts"):
            problems.append(
                {
                    "kind": "unresolved_native_edits",
                    "key": key,
                    "fields": ref["sync_conflicts"],
                }
            )
        archive_ref = ref.get("source_archive")
        if not archive_ref or not archive_ref.get("complete"):
            problems.append({"kind": "missing_or_incomplete_original", "key": key})
            continue
        if verify_archives:
            try:
                archive = read_source_archive(
                    archive_ref, actor_concept_id=actor_concept_id
                )
                if (
                    not archive.get("complete")
                    or str(archive["source"].get("id")) != str(issue.get("id"))
                    or archive["source"]["fields"].get("updated")
                    != issue["fields"].get("updated")
                ):
                    raise ValueError(
                        "Retained source identity/version/completeness mismatch"
                    )
                for item in archive.get("binaries", []):
                    data = base64.b64decode(
                        item["source"]["content_base64"], validate=True
                    )
                    if (
                        not item.get("complete")
                        or len(data) != item["size_bytes"]
                        or hashlib.sha256(data).hexdigest() != item["sha256"]
                    ):
                        raise ValueError(
                            "Retained attachment bytes do not match their manifest"
                        )
                verified.append(key)
                hashes.extend(
                    {
                        "issue_key": key,
                        "attachment_id": item["id"],
                        "sha256": item["sha256"],
                        "size_bytes": item["size_bytes"],
                    }
                    for item in archive.get("binaries", [])
                )
            except Exception as exc:
                problems.append(
                    {"kind": "archive_readback_failed", "key": key, "error": str(exc)}
                )
        for relation in source_relations[key]:
            target_key = relation["target_issue_key"]
            targets = target_by_key.get(target_key, [])
            if len(targets) != 1:
                problems.append(
                    {
                        "kind": "unresolved_relation_target",
                        "key": key,
                        "target_key": target_key,
                        "candidate_count": len(targets),
                    }
                )
                continue
            kind = relation["kind"]
            if kind == "parent":
                predicates = [PREDICATE_HAS_PARENT_TASK, PREDICATE_HAS_EPIC_TASK]
            elif kind == "epic":
                predicates = [PREDICATE_HAS_EPIC_TASK]
            else:
                predicate = TASK_LINK_TYPE_TO_PREDICATE.get(relation.get("link_type"))
                if not predicate:
                    problems.append(
                        {
                            "kind": "unmapped_source_link",
                            "key": key,
                            "relation": relation,
                        }
                    )
                    continue
                predicates = [predicate]
            represented = {
                value
                for predicate in predicates
                for value in doc.get("relationships", {}).get(predicate, [])
            }
            if targets[0]["concept_id"] not in represented:
                problems.append(
                    {"kind": "relation_mismatch", "key": key, "relation": relation}
                )
    project_archive = project.get("source_archive")
    if not project_archive or not project_archive.get("complete"):
        problems.append({"kind": "project_source_incomplete"})
    elif verify_archives:
        try:
            archived_project = read_source_archive(
                project_archive, actor_concept_id=actor_concept_id
            )
            if (
                not archived_project.get("complete")
                or str(archived_project["source"].get("id")) != project["source_id"]
            ):
                raise ValueError("Project archive identity or completeness mismatch")
        except Exception as exc:
            problems.append(
                {"kind": "project_archive_readback_failed", "error": str(exc)}
            )
    return {
        "schema": "jira_project_retention_reconciliation.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_concept_id": project_concept_id,
        "project_key": project_key,
        "source_issue_count": len(source_by_key),
        "represented_issue_count": len(set(source_by_key) & set(by_key)),
        "missing_issue_count": len(missing),
        "missing_issue_keys": missing,
        "missing_type_counts": dict(
            Counter(
                (source_by_key[key].get("fields", {}).get("issuetype") or {}).get(
                    "name", "Unknown"
                )
                for key in missing
            )
        ),
        "source_type_counts": dict(
            Counter(
                (issue.get("fields", {}).get("issuetype") or {}).get("name", "Unknown")
                for issue in source_by_key.values()
            )
        ),
        "verified_original_count": len(verified),
        "binary_hashes": hashes,
        "problems": problems,
        "retention_reconciled": verify_archives and not problems,
        "cutover_ready": False,
    }


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
