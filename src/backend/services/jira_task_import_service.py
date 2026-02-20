"""Import Jira issues into Von tasks with deterministic mapping reports.

This service maps Jira issue payloads (already fetched via Jira MCP proxy) to
Von task concepts using canonical task-management service operations. It is
designed for:
- dry-run previews,
- idempotent reruns keyed by Jira issue key,
- explicit mapped/dropped field reporting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Sequence

from .task_management_service import (
    InvalidTaskDataError,
    create_task,
    find_task_by_external_reference,
    link_tasks,
    set_task_epic,
    set_task_parent,
    update_task_fields,
    upsert_task_external_reference,
)


_JIRA_SOURCE_SYSTEM = "jira"
_DEFAULT_PRIORITY = "medium"
_DEFAULT_STATUS = "pending"

_JIRA_STATUS_TO_VON_STATUS = {
    "to do": "pending",
    "todo": "pending",
    "open": "pending",
    "selected for development": "pending",
    "backlog": "pending",
    "in progress": "in_progress",
    "doing": "in_progress",
    "in review": "in_progress",
    "review": "in_progress",
    "blocked": "blocked",
    "on hold": "blocked",
    "done": "completed",
    "closed": "completed",
    "resolved": "completed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}

_JIRA_PRIORITY_TO_VON_PRIORITY = {
    "highest": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "lowest": "low",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_issue_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def _extract_plain_text(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if not isinstance(value, Mapping):
        return None
    if value.get("type") == "text":
        text_value = value.get("text")
        if isinstance(text_value, str):
            return text_value
        return None

    chunks: list[str] = []
    content = value.get("content")
    if isinstance(content, list):
        for item in content:
            piece = _extract_plain_text(item)
            if isinstance(piece, str) and piece:
                chunks.append(piece)

    joined = " ".join(chunks).strip()
    return joined or None


def _extract_status_name(fields: Mapping[str, Any]) -> str | None:
    status = fields.get("status")
    if isinstance(status, Mapping):
        name = status.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(status, str) and status.strip():
        return status.strip()
    return None


def _extract_project_identity(fields: Mapping[str, Any]) -> tuple[str | None, str | None]:
    project = fields.get("project")
    if not isinstance(project, Mapping):
        return None, None

    project_key_raw = project.get("key")
    project_name_raw = project.get("name")
    project_key = (
        str(project_key_raw).strip().upper()
        if isinstance(project_key_raw, str) and project_key_raw.strip()
        else None
    )
    project_name = (
        str(project_name_raw).strip()
        if isinstance(project_name_raw, str) and project_name_raw.strip()
        else None
    )
    return project_key, project_name


def _extract_priority_name(fields: Mapping[str, Any]) -> str | None:
    priority = fields.get("priority")
    if isinstance(priority, Mapping):
        name = priority.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(priority, str) and priority.strip():
        return priority.strip()
    return None


def _map_status(status_name: str | None) -> tuple[str, str | None]:
    if not isinstance(status_name, str) or not status_name.strip():
        return _DEFAULT_STATUS, "jira_status_missing_defaulted_to_pending"
    normalised = status_name.strip().lower()
    mapped = _JIRA_STATUS_TO_VON_STATUS.get(normalised)
    if mapped:
        return mapped, None
    return _DEFAULT_STATUS, f"jira_status_unmapped:{status_name}"


def _map_priority(priority_name: str | None) -> tuple[str, str | None]:
    if not isinstance(priority_name, str) or not priority_name.strip():
        return _DEFAULT_PRIORITY, "jira_priority_missing_defaulted_to_medium"
    normalised = priority_name.strip().lower()
    mapped = _JIRA_PRIORITY_TO_VON_PRIORITY.get(normalised)
    if mapped:
        return mapped, None
    return _DEFAULT_PRIORITY, f"jira_priority_unmapped:{priority_name}"


def _normalise_labels(raw_labels: Any) -> list[str]:
    if not isinstance(raw_labels, list):
        return []
    seen: set[str] = set()
    labels: list[str] = []
    for raw in raw_labels:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        labels.append(cleaned)
    return labels


def _extract_named_values(raw_value: Any) -> list[str]:
    if not isinstance(raw_value, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        candidate: str | None = None
        if isinstance(item, Mapping):
            for key in ("name", "value", "key"):
                raw_candidate = item.get(key)
                if isinstance(raw_candidate, str) and raw_candidate.strip():
                    candidate = raw_candidate.strip()
                    break
        elif isinstance(item, str) and item.strip():
            candidate = item.strip()

        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        values.append(candidate)
    return values


def _extract_sprint_values(fields: Mapping[str, Any]) -> list[str]:
    for key in ("customfield_10020", "sprint", "sprints"):
        values = _extract_named_values(fields.get(key))
        if values:
            return values

        raw_value = fields.get(key)
        if isinstance(raw_value, str) and raw_value.strip():
            return [raw_value.strip()]
    return []


def _extract_rank_value(fields: Mapping[str, Any]) -> str | None:
    for key in ("customfield_10019", "customfield_10027", "rank"):
        raw_value = fields.get(key)
        if isinstance(raw_value, str) and raw_value.strip():
            return raw_value.strip()
        if isinstance(raw_value, Mapping):
            for candidate_key in ("rank", "value", "name"):
                candidate = raw_value.get(candidate_key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
    return None


def _normalise_datetime_or_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        if len(cleaned) == 10 and cleaned.count("-") == 2:
            try:
                parsed = datetime.fromisoformat(f"{cleaned}T00:00:00+00:00")
            except ValueError:
                return None
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _extract_parent_issue_key(fields: Mapping[str, Any]) -> str | None:
    parent = fields.get("parent")
    if isinstance(parent, Mapping):
        return _normalise_issue_key(parent.get("key"))
    return None


def _extract_epic_issue_key(fields: Mapping[str, Any]) -> str | None:
    candidate_keys = (
        "customfield_10014",
        "customfield_10008",
        "epic",
        "epic_link",
        "epicLink",
    )
    for key in candidate_keys:
        candidate = fields.get(key)
        if isinstance(candidate, Mapping):
            found = _normalise_issue_key(candidate.get("key"))
            if found:
                return found
            found = _normalise_issue_key(candidate.get("value"))
            if found:
                return found
        found = _normalise_issue_key(candidate)
        if found:
            return found
    return None


def _map_jira_link_phrase_to_type(phrase: Any) -> str | None:
    if not isinstance(phrase, str) or not phrase.strip():
        return None
    lowered = phrase.strip().lower()
    if "blocked by" in lowered:
        return "blocked_by"
    if "blocks" in lowered or "block" in lowered:
        return "blocks"
    if "depends on" in lowered:
        return "depends_on"
    if "depended on by" in lowered or "required by" in lowered:
        return "required_by"
    if "relat" in lowered:
        return "relates_to"
    return None


def _extract_issue_links(fields: Mapping[str, Any]) -> list[Dict[str, str]]:
    raw_links = fields.get("issuelinks")
    if not isinstance(raw_links, list):
        return []

    links: list[Dict[str, str]] = []
    for raw_link in raw_links:
        if not isinstance(raw_link, Mapping):
            continue
        link_type = raw_link.get("type")
        outward_phrase = None
        inward_phrase = None
        if isinstance(link_type, Mapping):
            outward_phrase = link_type.get("outward")
            inward_phrase = link_type.get("inward")

        outward_issue = raw_link.get("outwardIssue")
        if isinstance(outward_issue, Mapping):
            target_key = _normalise_issue_key(outward_issue.get("key"))
            mapped_type = _map_jira_link_phrase_to_type(outward_phrase)
            if target_key and mapped_type:
                links.append(
                    {
                        "target_issue_key": target_key,
                        "link_type": mapped_type,
                    }
                )

        inward_issue = raw_link.get("inwardIssue")
        if isinstance(inward_issue, Mapping):
            target_key = _normalise_issue_key(inward_issue.get("key"))
            mapped_type = _map_jira_link_phrase_to_type(inward_phrase)
            if target_key and mapped_type:
                links.append(
                    {
                        "target_issue_key": target_key,
                        "link_type": mapped_type,
                    }
                )
    return links


def _extract_status_history(issue: Mapping[str, Any]) -> tuple[list[Dict[str, Any]], str | None]:
    changelog = issue.get("changelog")
    if not isinstance(changelog, Mapping):
        return [], "jira_changelog_unavailable"

    histories = changelog.get("histories")
    if not isinstance(histories, list):
        return [], "jira_changelog_histories_missing"

    result: list[Dict[str, Any]] = []
    for history in histories:
        if not isinstance(history, Mapping):
            continue
        items = history.get("items")
        if not isinstance(items, list):
            continue
        created_at = history.get("created")
        author = history.get("author")
        author_display_name = None
        author_account_id = None
        if isinstance(author, Mapping):
            if isinstance(author.get("displayName"), str):
                author_display_name = author.get("displayName")
            if isinstance(author.get("accountId"), str):
                author_account_id = author.get("accountId")

        for item in items:
            if not isinstance(item, Mapping):
                continue
            field_name = str(item.get("field") or "").strip().lower()
            if field_name != "status":
                continue
            result.append(
                {
                    "from_status": item.get("fromString"),
                    "to_status": item.get("toString"),
                    "changed_at": created_at,
                    "author_display_name": author_display_name,
                    "author_account_id": author_account_id,
                }
            )
    if not result:
        return [], "jira_status_history_not_present"
    return result, None


def _extract_issue_fields(issue: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = issue.get("fields")
    if isinstance(fields, Mapping):
        return fields
    return {}


def _issue_reference_payload(
    issue: Mapping[str, Any],
    *,
    issue_key: str,
    labels: list[str],
    status_history: list[Dict[str, Any]],
    parent_issue_key: str | None,
    epic_issue_key: str | None,
    link_summaries: list[Dict[str, str]],
    assignee: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    fields = _extract_issue_fields(issue)
    project = fields.get("project")
    issue_type = fields.get("issuetype")
    status = fields.get("status")
    priority = fields.get("priority")
    payload: Dict[str, Any] = {
        "issue_key": issue_key,
        "issue_id": issue.get("id"),
        "issue_url": issue.get("self"),
        "project_key": project.get("key") if isinstance(project, Mapping) else None,
        "project_name": project.get("name") if isinstance(project, Mapping) else None,
        "issue_type": issue_type.get("name") if isinstance(issue_type, Mapping) else None,
        "status_name": status.get("name") if isinstance(status, Mapping) else None,
        "priority_name": priority.get("name") if isinstance(priority, Mapping) else None,
        "labels": labels,
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "due_date": fields.get("duedate"),
        "parent_issue_key": parent_issue_key,
        "epic_issue_key": epic_issue_key,
        "issue_links": link_summaries,
        "status_history": status_history,
        "imported_at": _iso_now(),
    }
    if isinstance(assignee, Mapping):
        payload["assignee"] = {
            "account_id": assignee.get("accountId"),
            "display_name": assignee.get("displayName"),
            "email_address": assignee.get("emailAddress"),
        }
    return payload


def _resolve_existing_task_id(
    *,
    jira_issue_key: str,
    organisation_concept_id: str | None,
    cache: dict[str, str | None],
) -> str | None:
    if jira_issue_key in cache:
        return cache[jira_issue_key]

    existing_task = find_task_by_external_reference(
        source_system=_JIRA_SOURCE_SYSTEM,
        external_id=jira_issue_key,
        organisation_concept_id=organisation_concept_id,
    )
    task_id = None
    if isinstance(existing_task, Mapping):
        raw_task_id = existing_task.get("task_concept_id")
        if isinstance(raw_task_id, str) and raw_task_id.strip():
            task_id = raw_task_id.strip()
    cache[jira_issue_key] = task_id
    return task_id


def _build_project_parity_report(
    project_observations: Mapping[str, Dict[str, Any]],
) -> Dict[str, Any]:
    project_rows: list[Dict[str, Any]] = []
    projects_with_follow_up = 0
    follow_up_items = 0

    for project_key in sorted(
        key for key in project_observations.keys() if isinstance(key, str)
    ):
        observation = project_observations.get(project_key) or {}
        issue_count = int(observation.get("issue_count", 0))
        status_names = sorted(
            str(item)
            for item in observation.get("status_names", set())
            if isinstance(item, str) and item
        )
        mapped_status_names = sorted(
            str(item)
            for item in observation.get("mapped_status_names", set())
            if isinstance(item, str) and item
        )
        unmapped_status_names = sorted(
            str(item)
            for item in observation.get("unmapped_status_names", set())
            if isinstance(item, str) and item
        )
        priority_names = sorted(
            str(item)
            for item in observation.get("priority_names", set())
            if isinstance(item, str) and item
        )
        mapped_priority_names = sorted(
            str(item)
            for item in observation.get("mapped_priority_names", set())
            if isinstance(item, str) and item
        )
        unmapped_priority_names = sorted(
            str(item)
            for item in observation.get("unmapped_priority_names", set())
            if isinstance(item, str) and item
        )
        component_names = sorted(
            str(item)
            for item in observation.get("component_names", set())
            if isinstance(item, str) and item
        )
        fix_version_names = sorted(
            str(item)
            for item in observation.get("fix_version_names", set())
            if isinstance(item, str) and item
        )
        sprint_values = sorted(
            str(item)
            for item in observation.get("sprint_values", set())
            if isinstance(item, str) and item
        )
        rank_values = sorted(
            str(item)
            for item in observation.get("rank_values", set())
            if isinstance(item, str) and item
        )

        dropped_fields: list[Dict[str, Any]] = []
        follow_up_work: list[str] = []
        if unmapped_status_names:
            dropped_fields.append(
                {
                    "field": "status_mapping",
                    "reason": "jira_statuses_unmapped_in_von_status_vocabulary",
                    "values": unmapped_status_names,
                }
            )
            follow_up_work.append(
                "Add/approve explicit Jira->Von status mapping for project workflow-specific states."
            )
        if unmapped_priority_names:
            dropped_fields.append(
                {
                    "field": "priority_mapping",
                    "reason": "jira_priorities_unmapped_in_von_priority_vocabulary",
                    "values": unmapped_priority_names,
                }
            )
            follow_up_work.append(
                "Extend Jira->Von priority mapping for project-specific priority names."
            )
        if component_names:
            dropped_fields.append(
                {
                    "field": "components",
                    "reason": "jira_components_not_first_class_in_von_task_model",
                    "values": component_names,
                }
            )
            follow_up_work.append(
                "Add first-class task component modelling and migration mapping."
            )
        if fix_version_names:
            dropped_fields.append(
                {
                    "field": "fixVersions",
                    "reason": "jira_fix_versions_not_first_class_in_von_task_model",
                    "values": fix_version_names,
                }
            )
            follow_up_work.append(
                "Add first-class release/fix-version modelling for tasks."
            )
        if sprint_values:
            dropped_fields.append(
                {
                    "field": "sprint",
                    "reason": "jira_sprint_metadata_not_first_class_in_von_task_model",
                    "values": sprint_values,
                }
            )
            follow_up_work.append(
                "Add first-class sprint/iteration modelling for task planning."
            )
        if rank_values:
            dropped_fields.append(
                {
                    "field": "rank",
                    "reason": "jira_rank_metadata_not_first_class_in_von_task_model",
                    "values": rank_values,
                }
            )
            follow_up_work.append(
                "Add first-class backlog rank/order modelling for tasks."
            )

        if follow_up_work:
            projects_with_follow_up += 1
            follow_up_items += len(follow_up_work)

        project_rows.append(
            {
                "project_key": (
                    project_key if project_key != "__unknown_project__" else None
                ),
                "project_name": (
                    observation.get("project_name")
                    if isinstance(observation.get("project_name"), str)
                    else None
                ),
                "issue_count": issue_count,
                "mapped_fields": [
                    "project.identity",
                    "status_mapping",
                    "priority_mapping",
                ],
                "dropped_fields": dropped_fields,
                "status_mapping": {
                    "observed_status_names": status_names,
                    "mapped_status_names": mapped_status_names,
                    "unmapped_status_names": unmapped_status_names,
                },
                "priority_mapping": {
                    "observed_priority_names": priority_names,
                    "mapped_priority_names": mapped_priority_names,
                    "unmapped_priority_names": unmapped_priority_names,
                },
                "follow_up_work": follow_up_work,
            }
        )

    return {
        "summary": {
            "projects_scanned": len(project_rows),
            "issues_scanned": sum(
                int(row.get("issue_count", 0))
                for row in project_rows
                if isinstance(row, dict)
            ),
            "projects_with_follow_up": projects_with_follow_up,
            "follow_up_items": follow_up_items,
        },
        "projects": project_rows,
    }


def import_jira_issues_to_tasks(
    *,
    issues: Sequence[Mapping[str, Any]],
    dry_run: bool = True,
    actor_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    assignee_account_id_to_concept_id: Mapping[str, str] | None = None,
    update_existing: bool = True,
) -> Dict[str, Any]:
    """Import Jira issues into Von tasks.

    Args:
        issues: Jira issue payloads (dict-like) from Jira MCP.
        dry_run: Preview-only mode. No writes are performed.
        actor_concept_id: Optional Von concept id of the importing actor.
        organisation_concept_id: Optional organisation scope for created tasks.
        assignee_account_id_to_concept_id: Optional map from Jira accountId to
            Von concept ids for assignee linkage.
        update_existing: When True, reruns update already-mapped tasks.
    """

    assignee_map = (
        {
            str(key).strip(): str(value).strip()
            for key, value in assignee_account_id_to_concept_id.items()
            if str(key).strip() and str(value).strip()
        }
        if isinstance(assignee_account_id_to_concept_id, Mapping)
        else {}
    )

    issue_results: list[Dict[str, Any]] = []
    existing_cache: dict[str, str | None] = {}
    task_id_by_issue_key: dict[str, str] = {}
    project_observations: dict[str, Dict[str, Any]] = {}

    summary = {
        "total_issues": 0,
        "created": 0,
        "updated": 0,
        "would_create": 0,
        "would_update": 0,
        "errors": 0,
        "mapped_fields": 0,
        "dropped_fields": 0,
        "mapped_relations": 0,
        "dropped_relations": 0,
    }

    # Pass 1: create/update base task records and persist external references.
    for raw_issue in issues:
        if not isinstance(raw_issue, Mapping):
            continue
        issue_key = _normalise_issue_key(raw_issue.get("key"))
        if not issue_key:
            continue
        summary["total_issues"] += 1

        fields = _extract_issue_fields(raw_issue)
        project_key, project_name = _extract_project_identity(fields)
        project_bucket_key = project_key or "__unknown_project__"
        project_observation = project_observations.setdefault(
            project_bucket_key,
            {
                "project_name": project_name,
                "issue_count": 0,
                "status_names": set(),
                "mapped_status_names": set(),
                "unmapped_status_names": set(),
                "priority_names": set(),
                "mapped_priority_names": set(),
                "unmapped_priority_names": set(),
                "component_names": set(),
                "fix_version_names": set(),
                "sprint_values": set(),
                "rank_values": set(),
            },
        )
        project_observation["issue_count"] = int(
            project_observation.get("issue_count", 0)
        ) + 1
        if project_name and not project_observation.get("project_name"):
            project_observation["project_name"] = project_name
        mapped_fields: list[str] = []
        dropped_fields: list[Dict[str, str]] = []
        relation_results: list[Dict[str, Any]] = []

        summary_text = fields.get("summary")
        if isinstance(summary_text, str) and summary_text.strip():
            title = summary_text.strip()
            mapped_fields.append("summary->title")
        else:
            title = f"Imported Jira issue {issue_key}"
            dropped_fields.append(
                {"field": "summary", "reason": "jira_summary_missing_fallback_used"}
            )

        description = _extract_plain_text(fields.get("description"))
        if description:
            mapped_fields.append("description")
        else:
            description = f"Imported from Jira issue {issue_key}."
            dropped_fields.append(
                {
                    "field": "description",
                    "reason": "jira_description_missing_fallback_used",
                }
            )

        jira_status_name = _extract_status_name(fields)
        mapped_status, status_warning = _map_status(jira_status_name)
        if jira_status_name:
            project_observation["status_names"].add(jira_status_name)
            if status_warning and status_warning.startswith("jira_status_unmapped"):
                project_observation["unmapped_status_names"].add(jira_status_name)
            else:
                project_observation["mapped_status_names"].add(jira_status_name)
        if status_warning:
            dropped_fields.append({"field": "status", "reason": status_warning})
        mapped_fields.append("status")

        jira_priority_name = _extract_priority_name(fields)
        mapped_priority, priority_warning = _map_priority(jira_priority_name)
        if jira_priority_name:
            project_observation["priority_names"].add(jira_priority_name)
            if priority_warning and priority_warning.startswith("jira_priority_unmapped"):
                project_observation["unmapped_priority_names"].add(jira_priority_name)
            else:
                project_observation["mapped_priority_names"].add(jira_priority_name)
        if priority_warning:
            dropped_fields.append({"field": "priority", "reason": priority_warning})
        mapped_fields.append("priority")

        labels = _normalise_labels(fields.get("labels"))
        if labels:
            mapped_fields.append("labels")

        due_date = _normalise_datetime_or_date(fields.get("duedate"))
        if fields.get("duedate") is not None and due_date is None:
            dropped_fields.append(
                {
                    "field": "due_date",
                    "reason": "jira_due_date_invalid_format",
                }
            )
        elif due_date:
            mapped_fields.append("due_date")

        start_date = _normalise_datetime_or_date(
            fields.get("startdate") or fields.get("startDate")
        )
        if start_date:
            mapped_fields.append("start_date")

        assignee = fields.get("assignee")
        assignee_concept_id = None
        if isinstance(assignee, Mapping):
            account_id = assignee.get("accountId")
            if isinstance(account_id, str) and account_id.strip():
                mapped = assignee_map.get(account_id.strip())
                if mapped:
                    assignee_concept_id = mapped
                    mapped_fields.append("assignee")
                else:
                    dropped_fields.append(
                        {
                            "field": "assignee",
                            "reason": "jira_assignee_mapping_missing",
                        }
                    )

        parent_issue_key = _extract_parent_issue_key(fields)
        if parent_issue_key:
            mapped_fields.append("hierarchy.parent")

        epic_issue_key = _extract_epic_issue_key(fields)
        if epic_issue_key:
            mapped_fields.append("hierarchy.epic")

        issue_links = _extract_issue_links(fields)
        if issue_links:
            mapped_fields.append("links")

        component_names = _extract_named_values(fields.get("components"))
        if component_names:
            project_observation["component_names"].update(component_names)

        fix_version_names = _extract_named_values(fields.get("fixVersions"))
        if fix_version_names:
            project_observation["fix_version_names"].update(fix_version_names)

        sprint_values = _extract_sprint_values(fields)
        if sprint_values:
            project_observation["sprint_values"].update(sprint_values)

        rank_value = _extract_rank_value(fields)
        if rank_value:
            project_observation["rank_values"].add(rank_value)

        status_history, status_history_warning = _extract_status_history(raw_issue)
        if status_history_warning:
            dropped_fields.append(
                {"field": "status_history", "reason": status_history_warning}
            )
        elif status_history:
            mapped_fields.append("status_history")

        existing_task_id = _resolve_existing_task_id(
            jira_issue_key=issue_key,
            organisation_concept_id=organisation_concept_id,
            cache=existing_cache,
        )

        task_id: str | None = None
        action: str

        update_fields_payload: Dict[str, Any] = {
            "status": mapped_status,
            "priority": mapped_priority,
            "labels": labels,
        }
        if due_date is not None:
            update_fields_payload["due_date"] = due_date
        if start_date is not None:
            update_fields_payload["start_date"] = start_date
        if assignee_concept_id is not None:
            update_fields_payload["assignee_concept_id"] = assignee_concept_id

        try:
            if existing_task_id:
                task_id = existing_task_id
                if dry_run:
                    action = "would_update"
                    summary["would_update"] += 1
                elif update_existing:
                    update_task_fields(
                        task_id,
                        fields=update_fields_payload,
                        actor_concept_id=actor_concept_id,
                    )
                    action = "updated"
                    summary["updated"] += 1
                else:
                    action = "skipped_existing"
                if not dry_run:
                    reference_payload = _issue_reference_payload(
                        raw_issue,
                        issue_key=issue_key,
                        labels=labels,
                        status_history=status_history,
                        parent_issue_key=parent_issue_key,
                        epic_issue_key=epic_issue_key,
                        link_summaries=issue_links,
                        assignee=assignee if isinstance(assignee, Mapping) else None,
                    )
                    upsert_task_external_reference(
                        task_id,
                        source_system=_JIRA_SOURCE_SYSTEM,
                        external_id=issue_key,
                        reference_payload=reference_payload,
                        actor_concept_id=actor_concept_id,
                    )
            else:
                if dry_run:
                    task_id = f"planned:{issue_key}"
                    action = "would_create"
                    summary["would_create"] += 1
                else:
                    created = create_task(
                        title=title,
                        description=description,
                        assignee_concept_id=assignee_concept_id,
                        created_by_concept_id=actor_concept_id,
                        organisation_concept_id=organisation_concept_id,
                    )
                    created_task_id = created.get("task_concept_id")
                    if not isinstance(created_task_id, str) or not created_task_id.strip():
                        raise InvalidTaskDataError(
                            f"create_task did not return task_concept_id for {issue_key}"
                        )
                    task_id = created_task_id.strip()
                    update_task_fields(
                        task_id,
                        fields=update_fields_payload,
                        actor_concept_id=actor_concept_id,
                    )
                    reference_payload = _issue_reference_payload(
                        raw_issue,
                        issue_key=issue_key,
                        labels=labels,
                        status_history=status_history,
                        parent_issue_key=parent_issue_key,
                        epic_issue_key=epic_issue_key,
                        link_summaries=issue_links,
                        assignee=assignee if isinstance(assignee, Mapping) else None,
                    )
                    upsert_task_external_reference(
                        task_id,
                        source_system=_JIRA_SOURCE_SYSTEM,
                        external_id=issue_key,
                        reference_payload=reference_payload,
                        actor_concept_id=actor_concept_id,
                    )
                    action = "created"
                    summary["created"] += 1
                    existing_cache[issue_key] = task_id
        except Exception as exc:
            action = "error"
            summary["errors"] += 1
            dropped_fields.append(
                {
                    "field": "import",
                    "reason": f"task_write_failed:{type(exc).__name__}:{exc}",
                }
            )

        if isinstance(task_id, str) and task_id:
            task_id_by_issue_key[issue_key] = task_id

        summary["mapped_fields"] += len(mapped_fields)
        summary["dropped_fields"] += len(dropped_fields)
        issue_results.append(
            {
                "jira_issue_key": issue_key,
                "task_concept_id": task_id,
                "action": action,
                "mapped_fields": mapped_fields,
                "dropped_fields": dropped_fields,
                "parent_issue_key": parent_issue_key,
                "epic_issue_key": epic_issue_key,
                "issue_links": issue_links,
                "relation_results": relation_results,
                "project_key": project_key,
                "project_name": project_name,
            }
        )

    # Pass 2: apply hierarchy and link relationships once task IDs are known.
    seen_links: set[tuple[str, str, str]] = set()
    for issue_row in issue_results:
        issue_key = issue_row.get("jira_issue_key")
        if not isinstance(issue_key, str):
            continue
        source_task_id = issue_row.get("task_concept_id")
        if not isinstance(source_task_id, str) or not source_task_id or issue_row.get(
            "action"
        ) == "error":
            continue
        if source_task_id.startswith("planned:") and dry_run:
            # Dry-run still reports relation intent, but does not mutate.
            pass

        relation_results_raw = issue_row.get("relation_results")
        if isinstance(relation_results_raw, list):
            relation_results_list: list[Dict[str, Any]] = relation_results_raw
        else:
            relation_results_list = []
            issue_row["relation_results"] = relation_results_list

        parent_issue_key = issue_row.get("parent_issue_key")
        if isinstance(parent_issue_key, str) and parent_issue_key:
            target_task_id = task_id_by_issue_key.get(parent_issue_key) or _resolve_existing_task_id(
                jira_issue_key=parent_issue_key,
                organisation_concept_id=organisation_concept_id,
                cache=existing_cache,
            )
            if isinstance(target_task_id, str) and target_task_id:
                try:
                    if not dry_run and not source_task_id.startswith("planned:"):
                        set_task_parent(
                            source_task_id,
                            target_task_id,
                            actor_concept_id=actor_concept_id,
                        )
                    relation_results_list.append(
                        {
                            "kind": "parent",
                            "target_issue_key": parent_issue_key,
                            "target_task_concept_id": target_task_id,
                            "status": "mapped" if not dry_run else "would_map",
                        }
                    )
                    summary["mapped_relations"] += 1
                except Exception as exc:
                    relation_results_list.append(
                        {
                            "kind": "parent",
                            "target_issue_key": parent_issue_key,
                            "status": "dropped",
                            "reason": f"parent_set_failed:{type(exc).__name__}:{exc}",
                        }
                    )
                    summary["dropped_relations"] += 1
            else:
                relation_results_list.append(
                    {
                        "kind": "parent",
                        "target_issue_key": parent_issue_key,
                        "status": "dropped",
                        "reason": "target_issue_not_imported",
                    }
                )
                summary["dropped_relations"] += 1

        epic_issue_key = issue_row.get("epic_issue_key")
        if isinstance(epic_issue_key, str) and epic_issue_key:
            target_task_id = task_id_by_issue_key.get(epic_issue_key) or _resolve_existing_task_id(
                jira_issue_key=epic_issue_key,
                organisation_concept_id=organisation_concept_id,
                cache=existing_cache,
            )
            if isinstance(target_task_id, str) and target_task_id:
                try:
                    if not dry_run and not source_task_id.startswith("planned:"):
                        set_task_epic(
                            source_task_id,
                            target_task_id,
                            actor_concept_id=actor_concept_id,
                        )
                    relation_results_list.append(
                        {
                            "kind": "epic",
                            "target_issue_key": epic_issue_key,
                            "target_task_concept_id": target_task_id,
                            "status": "mapped" if not dry_run else "would_map",
                        }
                    )
                    summary["mapped_relations"] += 1
                except Exception as exc:
                    relation_results_list.append(
                        {
                            "kind": "epic",
                            "target_issue_key": epic_issue_key,
                            "status": "dropped",
                            "reason": f"epic_set_failed:{type(exc).__name__}:{exc}",
                        }
                    )
                    summary["dropped_relations"] += 1
            else:
                relation_results_list.append(
                    {
                        "kind": "epic",
                        "target_issue_key": epic_issue_key,
                        "status": "dropped",
                        "reason": "target_issue_not_imported",
                    }
                )
                summary["dropped_relations"] += 1

        raw_links = issue_row.get("issue_links")
        links = raw_links if isinstance(raw_links, list) else []
        for link in links:
            if not isinstance(link, Mapping):
                continue
            target_issue_key = _normalise_issue_key(link.get("target_issue_key"))
            link_type = link.get("link_type")
            if not isinstance(target_issue_key, str) or not target_issue_key:
                continue
            if not isinstance(link_type, str) or not link_type.strip():
                relation_results_list.append(
                    {
                        "kind": "link",
                        "target_issue_key": target_issue_key,
                        "status": "dropped",
                        "reason": "unsupported_link_type",
                    }
                )
                summary["dropped_relations"] += 1
                continue

            target_task_id = task_id_by_issue_key.get(target_issue_key) or _resolve_existing_task_id(
                jira_issue_key=target_issue_key,
                organisation_concept_id=organisation_concept_id,
                cache=existing_cache,
            )
            if not isinstance(target_task_id, str) or not target_task_id:
                relation_results_list.append(
                    {
                        "kind": "link",
                        "target_issue_key": target_issue_key,
                        "link_type": link_type,
                        "status": "dropped",
                        "reason": "target_issue_not_imported",
                    }
                )
                summary["dropped_relations"] += 1
                continue

            dedupe_key = (source_task_id, target_task_id, link_type)
            if dedupe_key in seen_links:
                relation_results_list.append(
                    {
                        "kind": "link",
                        "target_issue_key": target_issue_key,
                        "target_task_concept_id": target_task_id,
                        "link_type": link_type,
                        "status": "skipped_duplicate",
                    }
                )
                continue
            seen_links.add(dedupe_key)

            try:
                if not dry_run and not source_task_id.startswith("planned:"):
                    link_tasks(
                        source_task_id,
                        target_task_id,
                        link_type=link_type,
                        actor_concept_id=actor_concept_id,
                    )
                relation_results_list.append(
                    {
                        "kind": "link",
                        "target_issue_key": target_issue_key,
                        "target_task_concept_id": target_task_id,
                        "link_type": link_type,
                        "status": "mapped" if not dry_run else "would_map",
                    }
                )
                summary["mapped_relations"] += 1
            except Exception as exc:
                relation_results_list.append(
                    {
                        "kind": "link",
                        "target_issue_key": target_issue_key,
                        "target_task_concept_id": target_task_id,
                        "link_type": link_type,
                        "status": "dropped",
                        "reason": f"link_failed:{type(exc).__name__}:{exc}",
                    }
                )
                summary["dropped_relations"] += 1

    return {
        "success": True,
        "source_system": _JIRA_SOURCE_SYSTEM,
        "dry_run": bool(dry_run),
        "update_existing": bool(update_existing),
        "summary": summary,
        "issues": issue_results,
        "project_parity": _build_project_parity_report(project_observations),
    }
