"""Import Jira issues into Von tasks with deterministic mapping reports.

This service maps Jira issue payloads (already fetched via Jira MCP proxy) to
Von task concepts using canonical task-management service operations. It is
designed for:
- dry-run previews,
- idempotent reruns keyed by Jira issue key,
- explicit mapped/dropped field reporting.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..utils.jira_issue_key_utils import normalise_jira_issue_key
from ..utils.concept_id_utils import canonicalise_vontology_concept_id, ensure_v_concept_prefix
from .concept_resolution_service import resolve_concept_by_name
from .concept_service import create_concept, get_concept_by_concept_id
from .text_value_service import upsert_text_for_concept
from .task_management_service import (
    InvalidTaskDataError,
    TASK_METADATA_KEY_EXTERNAL_REFERENCES,
    TASK_METADATA_KEY_ORGANISATION,
    TASK_SPECIFICATION_TYPE_ID,
    add_task_attachment,
    add_task_comment,
    add_task_worklog,
    create_task,
    find_task_by_external_reference,
    link_tasks,
    record_task_history_event,
    set_task_epic,
    set_task_parent,
    update_task_fields,
    upsert_task_external_reference,
)
from .task_ontology_service import JIRA_IMPORTED_TASK_SOURCE_ID

_normalise_issue_key = normalise_jira_issue_key

_JIRA_SOURCE_SYSTEM = "jira"
_DEFAULT_PRIORITY = "medium"
_DEFAULT_STATUS = "pending"
_JIRA_EMAIL_PREDICATE = "#V#has_email"
_JIRA_PARTICIPANT_REPORTER_METADATA_KEY = "jira_reporter_concept_id"
_JIRA_PARTICIPANT_WATCHERS_METADATA_KEY = "jira_watcher_concept_ids"

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
    "ready for qa": "in_progress",
    "qa": "in_progress",
    "suspended": "blocked",
    "blocked": "blocked",
    "on hold": "blocked",
    "done": "completed",
    "closed": "completed",
    "resolved": "completed",
    "superseded": "cancelled",
    "won't fix": "cancelled",
    "wont fix": "cancelled",
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


def _normalise_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
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


def _extract_issue_comments(issue: Mapping[str, Any]) -> list[Dict[str, Any]]:
    fields = _extract_issue_fields(issue)
    raw_comment = fields.get("comment") or issue.get("comment")
    if isinstance(raw_comment, Mapping):
        raw_comments = raw_comment.get("comments")
    elif isinstance(raw_comment, list):
        raw_comments = raw_comment
    else:
        raw_comments = None
    if not isinstance(raw_comments, list):
        return []

    result: list[Dict[str, Any]] = []
    for raw in raw_comments:
        if not isinstance(raw, Mapping):
            continue
        body = _extract_plain_text(raw.get("body"))
        if not isinstance(body, str) or not body.strip():
            continue
        comment_id = (
            str(raw.get("id")).strip()
            if raw.get("id") is not None and str(raw.get("id")).strip()
            else None
        )
        result.append(
            {
                "comment_id": comment_id,
                "body": body.strip(),
                "created_at": _normalise_datetime_or_date(raw.get("created")),
                "updated_at": _normalise_datetime_or_date(raw.get("updated")),
                "author": (
                    _extract_jira_participant(raw.get("author"))
                    if isinstance(raw.get("author"), Mapping)
                    else None
                ),
            }
        )
    return result


def _extract_issue_attachments(issue: Mapping[str, Any]) -> list[Dict[str, Any]]:
    fields = _extract_issue_fields(issue)
    raw_attachments = fields.get("attachment") or issue.get("attachment")
    if not isinstance(raw_attachments, list):
        return []

    result: list[Dict[str, Any]] = []
    for raw in raw_attachments:
        if not isinstance(raw, Mapping):
            continue
        attachment_id = (
            str(raw.get("id")).strip()
            if raw.get("id") is not None and str(raw.get("id")).strip()
            else None
        )
        filename = (
            str(raw.get("filename")).strip()
            if raw.get("filename") is not None and str(raw.get("filename")).strip()
            else None
        )
        if not filename:
            continue
        size_raw = raw.get("size")
        size_bytes: int | None = None
        if isinstance(size_raw, int):
            size_bytes = size_raw
        elif isinstance(size_raw, str) and size_raw.strip().isdigit():
            size_bytes = int(size_raw.strip())
        result.append(
            {
                "attachment_id": attachment_id,
                "filename": filename,
                "content_url": _normalise_optional_text(raw.get("content")),
                "thumbnail_url": _normalise_optional_text(raw.get("thumbnail")),
                "mime_type": _normalise_optional_text(
                    raw.get("mimeType") or raw.get("contentType")
                ),
                "size_bytes": size_bytes,
                "created_at": _normalise_datetime_or_date(raw.get("created")),
                "author": (
                    _extract_jira_participant(raw.get("author"))
                    if isinstance(raw.get("author"), Mapping)
                    else None
                ),
                "content_base64": _normalise_optional_text(raw.get("content_base64")),
            }
        )
    return result


def _extract_issue_worklog(issue: Mapping[str, Any]) -> list[Dict[str, Any]]:
    fields = _extract_issue_fields(issue)
    raw_worklog = fields.get("worklog") or issue.get("worklog")
    if isinstance(raw_worklog, Mapping):
        raw_entries = raw_worklog.get("worklogs")
    elif isinstance(raw_worklog, list):
        raw_entries = raw_worklog
    else:
        raw_entries = None
    if not isinstance(raw_entries, list):
        return []

    result: list[Dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            continue
        worklog_id = (
            str(raw.get("id")).strip()
            if raw.get("id") is not None and str(raw.get("id")).strip()
            else None
        )
        time_spent_seconds = raw.get("timeSpentSeconds")
        seconds_value = (
            time_spent_seconds if isinstance(time_spent_seconds, int) else None
        )
        if seconds_value is None and isinstance(time_spent_seconds, str):
            cleaned = time_spent_seconds.strip()
            if cleaned.isdigit():
                seconds_value = int(cleaned)
        result.append(
            {
                "worklog_id": worklog_id,
                "time_spent_seconds": seconds_value,
                "comment": _extract_plain_text(raw.get("comment")),
                "created_at": _normalise_datetime_or_date(raw.get("created")),
                "started_at": _normalise_datetime_or_date(raw.get("started")),
                "author": (
                    _extract_jira_participant(raw.get("author"))
                    if isinstance(raw.get("author"), Mapping)
                    else None
                ),
            }
        )
    return result


def _normalise_positive_minutes_from_seconds(value: Any) -> int | None:
    if not isinstance(value, int) or value <= 0:
        return None
    return max(1, int(math.ceil(value / 60.0)))


def _build_import_signature(prefix: str, *parts: Any) -> str:
    joined = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:16]}"


def _extract_issue_fields(issue: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = issue.get("fields")
    if isinstance(fields, Mapping):
        return fields
    return {}


def _normalise_jira_account_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def _extract_jira_participant(raw_value: Any) -> Mapping[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    account_id = _normalise_jira_account_id(
        raw_value.get("accountId") or raw_value.get("account_id")
    )
    email = _normalise_email(raw_value.get("emailAddress") or raw_value.get("email_address"))
    display_name_raw = (
        raw_value.get("displayName")
        or raw_value.get("display_name")
        or raw_value.get("name")
    )
    display_name = (
        str(display_name_raw).strip()
        if isinstance(display_name_raw, str) and str(display_name_raw).strip()
        else None
    )
    if not account_id and not email and not display_name:
        return None
    return {
        "account_id": account_id,
        "email_address": email,
        "display_name": display_name,
        "active": (
            bool(raw_value.get("active")) if isinstance(raw_value.get("active"), bool) else None
        ),
    }


def _extract_jira_watcher_participants(
    issue: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], int | None]:
    containers: list[Any] = [
        issue.get("watchers"),
        issue.get("watcher"),
        fields.get("watchers"),
        fields.get("watcher"),
        fields.get("watches"),
    ]

    watch_count: int | None = None
    watchers: list[Mapping[str, Any]] = []
    for container in containers:
        if isinstance(container, Mapping):
            raw_count = container.get("watchCount")
            if isinstance(raw_count, int) and raw_count >= 0:
                watch_count = raw_count

            if isinstance(container.get("watchers"), list):
                watchers.extend(
                    item for item in container.get("watchers", []) if isinstance(item, Mapping)
                )
            if isinstance(container.get("items"), list):
                watchers.extend(
                    item for item in container.get("items", []) if isinstance(item, Mapping)
                )
            if (
                isinstance(container.get("accountId"), str)
                or isinstance(container.get("emailAddress"), str)
                or isinstance(container.get("displayName"), str)
            ):
                watchers.append(container)
        elif isinstance(container, list):
            watchers.extend(item for item in container if isinstance(item, Mapping))

    deduped: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for watcher in watchers:
        participant = _extract_jira_participant(watcher)
        if participant is None:
            continue
        account_id = participant.get("account_id")
        email = participant.get("email_address")
        display_name = participant.get("display_name")
        dedupe_key = (
            str(account_id).strip().lower()
            if isinstance(account_id, str) and account_id.strip()
            else (
                str(email).strip().lower()
                if isinstance(email, str) and email.strip()
                else str(display_name).strip().lower()
            )
        )
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(participant)

    return deduped, watch_count


def _resolve_concept_id_by_text_relation(
    *,
    predicate: str,
    text: str,
) -> str | None:
    if not isinstance(predicate, str) or not predicate.strip():
        return None
    if not isinstance(text, str) or not text.strip():
        return None

    escaped = re.escape(text.strip())
    text_docs = list(
        TextValuesRepository.find(
            {"text": {"$regex": f"^{escaped}$", "$options": "i"}},
            projection={"_id": 1},
            limit=20,
        )
    )
    if not text_docs:
        return None

    candidate_ids: set[str] = set()
    predicate_values = [predicate]
    if predicate.startswith("#V#"):
        predicate_values.append(predicate.replace("#V#", "", 1))
    else:
        predicate_values.append(f"#V#{predicate}")

    for text_doc in text_docs:
        text_value_id = text_doc.get("_id")
        if text_value_id is None:
            continue
        relation_rows = list(
            TextRelationsRepository.find(
                {
                    "object_text_id": {"$in": [text_value_id, str(text_value_id)]},
                    "predicate": {"$in": predicate_values},
                },
                projection={"subject_concept_id": 1},
                limit=20,
            )
        )
        for relation in relation_rows:
            subject_concept_id = relation.get("subject_concept_id")
            if not isinstance(subject_concept_id, str) or not subject_concept_id.strip():
                continue
            candidate_ids.add(subject_concept_id.strip())

    if not candidate_ids:
        return None

    for candidate_id in sorted(candidate_ids):
        if ConceptsRepository.find_one({"concept_id": candidate_id}, {"_id": 1}):
            return candidate_id
    return None


def _resolve_existing_person_concept_id(
    *,
    account_id: str | None,
    email_address: str | None,
    display_name: str | None,
) -> str | None:
    if account_id:
        try:
            account_resolution = resolve_concept_by_name(
                name=account_id,
                instance_of="#V#person",
                match_code_strings=False,
                max_results=5,
            )
        except Exception:
            account_resolution = {}
        if (
            isinstance(account_resolution, Mapping)
            and account_resolution.get("status") == "resolved"
        ):
            resolved_id = account_resolution.get("resolved_concept_id")
            if isinstance(resolved_id, str) and resolved_id.strip():
                return resolved_id.strip()

    if email_address:
        resolved_by_email = _resolve_concept_id_by_text_relation(
            predicate=_JIRA_EMAIL_PREDICATE,
            text=email_address,
        )
        if resolved_by_email:
            return resolved_by_email

    if display_name:
        try:
            name_resolution = resolve_concept_by_name(
                name=display_name,
                instance_of="#V#person",
                match_code_strings=False,
                max_results=5,
            )
        except Exception:
            name_resolution = {}
        if isinstance(name_resolution, Mapping) and name_resolution.get("status") == "resolved":
            resolved_id = name_resolution.get("resolved_concept_id")
            if isinstance(resolved_id, str) and resolved_id.strip():
                return resolved_id.strip()

    return None


def _build_jira_person_concept_id(
    *,
    account_id: str | None,
    email_address: str | None,
    display_name: str | None,
) -> str:
    seed = account_id or email_address or display_name or f"participant_{uuid.uuid4().hex[:8]}"
    canonical = canonicalise_vontology_concept_id(f"jira_person_{seed}")
    if canonical:
        return canonical
    return f"#V#jira_person_{uuid.uuid4().hex[:8]}"


def _upsert_jira_participant_aliases(
    *,
    concept_id: str,
    account_id: str | None,
    email_address: str | None,
) -> None:
    if account_id:
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            text=account_id,
            lang="en-NZ",
            context={"name_type": "CODE", "source": "jira_account_id"},
        )
    if email_address:
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate=_JIRA_EMAIL_PREDICATE,
            text=email_address,
            lang="en-NZ",
            context={"source": "jira_account_profile"},
        )


def _ensure_jira_participant_concept(
    *,
    raw_participant: Mapping[str, Any] | None,
    account_id_to_concept_id: Mapping[str, str],
    account_cache: Dict[str, str],
    email_cache: Dict[str, str],
    actor_concept_id: str | None,
    organisation_concept_id: str | None,
    allow_lookup: bool,
    allow_create: bool,
) -> Dict[str, Any]:
    participant = _extract_jira_participant(raw_participant)
    if participant is None:
        return {"concept_id": None, "resolution": "no_participant_payload"}

    account_id = participant.get("account_id")
    email_address = participant.get("email_address")
    display_name = participant.get("display_name")

    mapped_concept_id: str | None = None
    resolution = "unresolved"

    if isinstance(account_id, str) and account_id:
        explicit_mapped = account_id_to_concept_id.get(account_id)
        if explicit_mapped:
            mapped_concept_id = explicit_mapped
            resolution = "mapped_from_account_map"

    if mapped_concept_id is None and isinstance(account_id, str) and account_id:
        cached = account_cache.get(account_id)
        if cached:
            mapped_concept_id = cached
            resolution = "mapped_from_account_cache"

    if mapped_concept_id is None and isinstance(email_address, str) and email_address:
        cached_email = email_cache.get(email_address)
        if cached_email:
            mapped_concept_id = cached_email
            resolution = "mapped_from_email_cache"

    if mapped_concept_id is None and allow_lookup:
        resolved = _resolve_existing_person_concept_id(
            account_id=account_id if isinstance(account_id, str) else None,
            email_address=email_address if isinstance(email_address, str) else None,
            display_name=display_name if isinstance(display_name, str) else None,
        )
        if isinstance(resolved, str) and resolved.strip():
            mapped_concept_id = resolved.strip()
            resolution = "mapped_from_existing_concept"

    created_concept = False
    if mapped_concept_id is None and allow_create:
        concept_name = (
            display_name
            if isinstance(display_name, str) and display_name
            else (
                email_address
                if isinstance(email_address, str) and email_address
                else (
                    f"Jira user {account_id}"
                    if isinstance(account_id, str) and account_id
                    else "Jira user"
                )
            )
        )
        candidate_concept_id = _build_jira_person_concept_id(
            account_id=account_id if isinstance(account_id, str) else None,
            email_address=email_address if isinstance(email_address, str) else None,
            display_name=display_name if isinstance(display_name, str) else None,
        )
        try:
            created = create_concept(
                name=concept_name,
                concept_id=candidate_concept_id,
                parent_concept_ids=["#V#person"],
                create_as_instance=True,
                created_by_concept_id=actor_concept_id,
                organisation_concept_id=organisation_concept_id,
            )
            created_concept_id = created.get("concept_id") if isinstance(created, Mapping) else None
            if isinstance(created_concept_id, str) and created_concept_id.strip():
                mapped_concept_id = created_concept_id.strip()
            else:
                mapped_concept_id = candidate_concept_id
            created_concept = True
            resolution = "created_new_concept"
        except Exception:
            existing = get_concept_by_concept_id(candidate_concept_id)
            existing_concept_id = existing.get("concept_id") if isinstance(existing, Mapping) else None
            if isinstance(existing_concept_id, str) and existing_concept_id.strip():
                mapped_concept_id = existing_concept_id.strip()
                resolution = "mapped_existing_candidate_concept"

    if isinstance(mapped_concept_id, str) and mapped_concept_id:
        mapped_concept_id = ensure_v_concept_prefix(mapped_concept_id) or mapped_concept_id
        try:
            _upsert_jira_participant_aliases(
                concept_id=mapped_concept_id,
                account_id=account_id if isinstance(account_id, str) else None,
                email_address=(
                    email_address if isinstance(email_address, str) and email_address else None
                ),
            )
        except Exception:
            # Alias persistence is best-effort; mapping should still proceed.
            pass

        if isinstance(account_id, str) and account_id:
            account_cache[account_id] = mapped_concept_id
        if isinstance(email_address, str) and email_address:
            email_cache[email_address] = mapped_concept_id

    return {
        "concept_id": mapped_concept_id,
        "resolution": resolution,
        "created_concept": created_concept,
        "participant": participant,
    }


def _participant_reference_payload(
    *,
    participant: Mapping[str, Any] | None,
    concept_id: str | None,
) -> Dict[str, Any] | None:
    if not isinstance(participant, Mapping):
        return None
    payload = {
        "account_id": participant.get("account_id"),
        "display_name": participant.get("display_name"),
        "email_address": participant.get("email_address"),
        "concept_id": concept_id,
    }
    return payload


def _issue_reference_payload(
    issue: Mapping[str, Any],
    *,
    issue_key: str,
    labels: list[str],
    component_names: list[str],
    fix_version_names: list[str],
    sprint_values: list[str],
    rank_value: str | None,
    status_history: list[Dict[str, Any]],
    parent_issue_key: str | None,
    epic_issue_key: str | None,
    link_summaries: list[Dict[str, str]],
    assignee: Mapping[str, Any] | None,
    creator: Mapping[str, Any] | None,
    reporter: Mapping[str, Any] | None,
    watcher_participants: list[Mapping[str, Any]],
    watcher_count: int | None,
    assignee_concept_id: str | None,
    creator_concept_id: str | None,
    reporter_concept_id: str | None,
    watcher_concept_ids: list[str],
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
        "components": component_names,
        "fix_versions": fix_version_names,
        "sprint_values": sprint_values,
        "backlog_rank": rank_value,
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "due_date": fields.get("duedate"),
        "parent_issue_key": parent_issue_key,
        "epic_issue_key": epic_issue_key,
        "issue_links": link_summaries,
        "status_history": status_history,
        "imported_at": _iso_now(),
    }

    assignee_payload = _participant_reference_payload(
        participant=assignee,
        concept_id=assignee_concept_id,
    )
    creator_payload = _participant_reference_payload(
        participant=creator,
        concept_id=creator_concept_id,
    )
    reporter_payload = _participant_reference_payload(
        participant=reporter,
        concept_id=reporter_concept_id,
    )

    watcher_payloads = [
        _participant_reference_payload(
            participant=watcher,
            concept_id=watcher_concept_ids[idx] if idx < len(watcher_concept_ids) else None,
        )
        for idx, watcher in enumerate(watcher_participants)
        if isinstance(watcher, Mapping)
    ]
    watcher_payloads = [item for item in watcher_payloads if isinstance(item, dict)]

    payload["participants"] = {
        "assignee": assignee_payload,
        "creator": creator_payload,
        "reporter": reporter_payload,
        "watchers": watcher_payloads,
        "watcher_count": watcher_count,
    }
    payload[_JIRA_PARTICIPANT_REPORTER_METADATA_KEY] = reporter_concept_id
    payload[_JIRA_PARTICIPANT_WATCHERS_METADATA_KEY] = watcher_concept_ids

    if assignee_payload:
        payload["assignee"] = assignee_payload
    if creator_payload:
        payload["creator"] = creator_payload
    if reporter_payload:
        payload["reporter"] = reporter_payload
    if watcher_payloads:
        payload["watchers"] = watcher_payloads

    return payload


def _existing_imported_activity_checkpoint(
    existing_task: Mapping[str, Any] | None,
) -> dict[str, set[str]]:
    external_references = (
        existing_task.get("external_references")
        if isinstance(existing_task, Mapping)
        else None
    )
    jira_reference = (
        external_references.get(_JIRA_SOURCE_SYSTEM)
        if isinstance(external_references, Mapping)
        else None
    )
    imported_activity = (
        jira_reference.get("imported_activity")
        if isinstance(jira_reference, Mapping)
        else None
    )

    def _extract_set(key: str) -> set[str]:
        values = imported_activity.get(key) if isinstance(imported_activity, Mapping) else None
        if not isinstance(values, list):
            return set()
        return {
            str(value).strip()
            for value in values
            if isinstance(value, str) and str(value).strip()
        }

    return {
        "comment_ids": _extract_set("comment_ids"),
        "attachment_ids": _extract_set("attachment_ids"),
        "worklog_ids": _extract_set("worklog_ids"),
        "status_history_signatures": _extract_set("status_history_signatures"),
    }


def _jira_source_entry(
    *,
    issue_key: str,
    external_id: str | None,
    created_at: str | None = None,
    content_url: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_system": _JIRA_SOURCE_SYSTEM,
        "issue_key": issue_key,
    }
    if isinstance(external_id, str) and external_id.strip():
        payload["external_id"] = external_id.strip()
    if isinstance(created_at, str) and created_at.strip():
        payload["created_at"] = created_at.strip()
    if isinstance(content_url, str) and content_url.strip():
        payload["content_url"] = content_url.strip()
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            if value is None:
                continue
            payload[str(key)] = value
    return payload


def _materialise_jira_issue_activity(
    *,
    task_concept_id: str,
    issue_key: str,
    raw_issue: Mapping[str, Any],
    actor_concept_id: str | None,
    organisation_concept_id: str | None,
    namespace: str | None,
    participant_map: Mapping[str, str],
    participant_account_cache: Dict[str, str],
    participant_email_cache: Dict[str, str],
    auto_resolve_participants: bool,
    create_missing_participant_concepts: bool,
    dry_run: bool,
    existing_activity_checkpoint: dict[str, set[str]],
) -> tuple[dict[str, list[str]], list[str], list[Dict[str, str]]]:
    imported_activity = {
        key: sorted(set(values)) for key, values in existing_activity_checkpoint.items()
    }
    mapped_fields: list[str] = []
    dropped_fields: list[Dict[str, str]] = []

    def _resolve_participant(raw_participant: Mapping[str, Any] | None) -> dict[str, Any]:
        return _ensure_jira_participant_concept(
            raw_participant=raw_participant,
            account_id_to_concept_id=participant_map,
            account_cache=participant_account_cache,
            email_cache=participant_email_cache,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            allow_lookup=bool(auto_resolve_participants),
            allow_create=(
                bool(auto_resolve_participants)
                and bool(create_missing_participant_concepts)
                and not dry_run
            ),
        )

    comments = _extract_issue_comments(raw_issue)
    imported_comment = False
    for comment in comments:
        raw_comment_id = comment.get("comment_id")
        if isinstance(raw_comment_id, str):
            comment_id = raw_comment_id
        else:
            comment_id = _build_import_signature(
                "jira_comment",
                issue_key,
                comment.get("created_at"),
                comment.get("body"),
            )
        if comment_id in existing_activity_checkpoint["comment_ids"]:
            continue
        imported_comment = True
        if dry_run:
            imported_activity["comment_ids"].append(comment_id)
            continue
        author_resolution = _resolve_participant(
            comment.get("author") if isinstance(comment.get("author"), Mapping) else None
        )
        author_concept_id = (
            author_resolution.get("concept_id")
            if isinstance(author_resolution.get("concept_id"), str)
            else None
        )
        try:
            add_task_comment(
                task_concept_id,
                body=str(comment.get("body")),
                author_concept_id=author_concept_id,
                created_at=(
                    comment.get("created_at")
                    if isinstance(comment.get("created_at"), str)
                    else None
                ),
                source=_jira_source_entry(
                    issue_key=issue_key,
                    external_id=comment_id,
                    created_at=(
                        comment.get("created_at")
                        if isinstance(comment.get("created_at"), str)
                        else None
                    ),
                ),
            )
            imported_activity["comment_ids"].append(comment_id)
        except Exception as exc:
            dropped_fields.append(
                {
                    "field": "comments",
                    "reason": f"jira_comment_import_failed:{comment_id}:{type(exc).__name__}:{exc}",
                }
            )
    if imported_comment:
        mapped_fields.append("comments")

    attachments = _extract_issue_attachments(raw_issue)
    imported_attachment = False
    for attachment in attachments:
        raw_attachment_id = attachment.get("attachment_id")
        if isinstance(raw_attachment_id, str):
            attachment_id = raw_attachment_id
        else:
            attachment_id = _build_import_signature(
                "jira_attachment",
                issue_key,
                attachment.get("filename"),
                attachment.get("created_at"),
                attachment.get("size_bytes"),
            )
        if attachment_id in existing_activity_checkpoint["attachment_ids"]:
            continue
        imported_attachment = True
        if dry_run:
            imported_activity["attachment_ids"].append(attachment_id)
            continue
        author_resolution = _resolve_participant(
            attachment.get("author")
            if isinstance(attachment.get("author"), Mapping)
            else None
        )
        author_concept_id = (
            author_resolution.get("concept_id")
            if isinstance(author_resolution.get("concept_id"), str)
            else None
        )
        attachment_uri = (
            attachment.get("content_url")
            if isinstance(attachment.get("content_url"), str)
            else None
        )
        file_copy_concept_id: str | None = None
        content_base64 = (
            attachment.get("content_base64")
            if isinstance(attachment.get("content_base64"), str)
            else None
        )
        if content_base64:
            try:
                attachment_bytes = base64.b64decode(content_base64, validate=True)
                from .computer_file_copy_service import import_bytes_file_copy

                file_copy_result = import_bytes_file_copy(
                    data=attachment_bytes,
                    user_concept_id=actor_concept_id or "",
                    organisation_concept_id=organisation_concept_id,
                    namespace=namespace,
                    original_filename=str(attachment.get("filename")),
                    content_type=(
                        attachment.get("mime_type")
                        if isinstance(attachment.get("mime_type"), str)
                        else None
                    ),
                    source_system="jira_attachment",
                    source_identifier=attachment_id,
                    source_uri=attachment_uri,
                    metadata={"jira_issue_key": issue_key},
                )
                if file_copy_result.get("success") is True:
                    concept_id_value = file_copy_result.get("concept_id")
                    if isinstance(concept_id_value, str) and concept_id_value.strip():
                        file_copy_concept_id = concept_id_value.strip()
                    storage = file_copy_result.get("storage")
                    if isinstance(storage, Mapping):
                        storage_uri = storage.get("uri")
                        if isinstance(storage_uri, str) and storage_uri.strip():
                            attachment_uri = storage_uri.strip()
                else:
                    dropped_fields.append(
                        {
                            "field": "attachments",
                            "reason": (
                                "jira_attachment_blob_import_failed:"
                                f"{attachment_id}:{file_copy_result.get('error')}"
                            ),
                        }
                    )
            except Exception as exc:
                dropped_fields.append(
                    {
                        "field": "attachments",
                        "reason": (
                            "jira_attachment_blob_import_failed:"
                            f"{attachment_id}:{type(exc).__name__}:{exc}"
                        ),
                    }
                )
        try:
            add_task_attachment(
                task_concept_id,
                filename=str(attachment.get("filename")),
                uri=attachment_uri or f"jira://attachment/{attachment_id}",
                media_type=(
                    attachment.get("mime_type")
                    if isinstance(attachment.get("mime_type"), str)
                    else None
                ),
                size_bytes=(
                    attachment.get("size_bytes")
                    if isinstance(attachment.get("size_bytes"), int)
                    else None
                ),
                added_by_concept_id=author_concept_id,
                created_at=(
                    attachment.get("created_at")
                    if isinstance(attachment.get("created_at"), str)
                    else None
                ),
                source=_jira_source_entry(
                    issue_key=issue_key,
                    external_id=attachment_id,
                    created_at=(
                        attachment.get("created_at")
                        if isinstance(attachment.get("created_at"), str)
                        else None
                    ),
                    content_url=(
                        attachment.get("content_url")
                        if isinstance(attachment.get("content_url"), str)
                        else None
                    ),
                ),
                file_copy_concept_id=file_copy_concept_id,
            )
            imported_activity["attachment_ids"].append(attachment_id)
        except Exception as exc:
            dropped_fields.append(
                {
                    "field": "attachments",
                    "reason": (
                        "jira_attachment_metadata_import_failed:"
                        f"{attachment_id}:{type(exc).__name__}:{exc}"
                    ),
                }
            )
    if imported_attachment:
        mapped_fields.append("attachments")

    worklog_entries = _extract_issue_worklog(raw_issue)
    imported_worklog = False
    for worklog in worklog_entries:
        raw_worklog_id = worklog.get("worklog_id")
        if isinstance(raw_worklog_id, str):
            worklog_id = raw_worklog_id
        else:
            worklog_id = _build_import_signature(
                "jira_worklog",
                issue_key,
                worklog.get("created_at"),
                worklog.get("started_at"),
                worklog.get("time_spent_seconds"),
            )
        if worklog_id in existing_activity_checkpoint["worklog_ids"]:
            continue
        imported_worklog = True
        imported_activity["worklog_ids"].append(worklog_id)
        minutes = _normalise_positive_minutes_from_seconds(
            worklog.get("time_spent_seconds")
        )
        if minutes is None:
            dropped_fields.append(
                {
                    "field": "worklog",
                    "reason": f"jira_worklog_duration_missing:{worklog_id}",
                }
            )
            continue
        if dry_run:
            continue
        author_resolution = _resolve_participant(
            worklog.get("author") if isinstance(worklog.get("author"), Mapping) else None
        )
        author_concept_id = (
            author_resolution.get("concept_id")
            if isinstance(author_resolution.get("concept_id"), str)
            else None
        )
        try:
            add_task_worklog(
                task_concept_id,
                time_spent_minutes=minutes,
                author_concept_id=author_concept_id,
                comment=(
                    worklog.get("comment")
                    if isinstance(worklog.get("comment"), str)
                    else None
                ),
                started_at=(
                    worklog.get("started_at")
                    if isinstance(worklog.get("started_at"), str)
                    else None
                ),
                created_at=(
                    worklog.get("created_at")
                    if isinstance(worklog.get("created_at"), str)
                    else None
                ),
                source=_jira_source_entry(
                    issue_key=issue_key,
                    external_id=worklog_id,
                    created_at=(
                        worklog.get("created_at")
                        if isinstance(worklog.get("created_at"), str)
                        else None
                    ),
                ),
            )
        except Exception as exc:
            dropped_fields.append(
                {
                    "field": "worklog",
                    "reason": (
                        "jira_worklog_import_failed:"
                        f"{worklog_id}:{type(exc).__name__}:{exc}"
                    ),
                }
            )
    if imported_worklog:
        mapped_fields.append("worklog")

    status_history, _warning = _extract_status_history(raw_issue)
    imported_status_history = False
    for history_row in status_history:
        signature: str = _build_import_signature(
            "jira_status_history",
            issue_key,
            history_row.get("changed_at"),
            history_row.get("from_status"),
            history_row.get("to_status"),
            history_row.get("author_account_id"),
            history_row.get("author_display_name"),
        )
        if signature in existing_activity_checkpoint["status_history_signatures"]:
            continue
        imported_status_history = True
        imported_activity["status_history_signatures"].append(signature)
        if dry_run:
            continue
        author_resolution = _resolve_participant(
            {
                "accountId": history_row.get("author_account_id"),
                "displayName": history_row.get("author_display_name"),
            }
        )
        author_concept_id = (
            author_resolution.get("concept_id")
            if isinstance(author_resolution.get("concept_id"), str)
            else None
        )
        try:
            record_task_history_event(
                task_concept_id,
                event_type="task_status_transition_imported",
                actor_concept_id=author_concept_id,
                event_timestamp=(
                    history_row.get("changed_at")
                    if isinstance(history_row.get("changed_at"), str)
                    else None
                ),
                details={
                    "source_system": _JIRA_SOURCE_SYSTEM,
                    "issue_key": issue_key,
                    "from_status": history_row.get("from_status"),
                    "to_status": history_row.get("to_status"),
                    "author_account_id": history_row.get("author_account_id"),
                    "author_display_name": history_row.get("author_display_name"),
                    "import_signature": signature,
                },
            )
        except Exception as exc:
            dropped_fields.append(
                {
                    "field": "status_history",
                    "reason": (
                        "jira_status_history_import_failed:"
                        f"{signature}:{type(exc).__name__}:{exc}"
                    ),
                }
            )
    if imported_status_history:
        mapped_fields.append("status_history.imported")

    imported_activity = {
        key: sorted(
            {
                str(value).strip()
                for value in values
                if isinstance(value, str) and str(value).strip()
            }
        )
        for key, values in imported_activity.items()
    }
    return imported_activity, mapped_fields, dropped_fields


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


def list_imported_jira_issue_keys(
    *,
    organisation_concept_id: str | None = None,
    limit: int | None = None,
) -> list[str]:
    """Return Jira issue keys already linked via task external_references.

    This intentionally uses a distinct-value query rather than a sorted scan of
    task documents. Full-document scans were operationally unreliable for
    project-scale migration checkpoints and incremental sync runs.
    """

    bounded_limit: int | None = None
    if isinstance(limit, int):
        bounded_limit = max(1, min(limit, 100000))

    query: Dict[str, Any] = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
        f"metadata.{TASK_METADATA_KEY_EXTERNAL_REFERENCES}.{_JIRA_SOURCE_SYSTEM}.external_id": {
            "$exists": True
        },
    }
    if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
        org_scope = ensure_v_concept_prefix(organisation_concept_id)
        # Include legacy unscoped task rows so organisation-scoped backfill can
        # discover and repair prior imports written without organisation context.
        query["$or"] = [
            {f"metadata.{TASK_METADATA_KEY_ORGANISATION}": org_scope},
            {f"metadata.{TASK_METADATA_KEY_ORGANISATION}": None},
        ]

    raw_values = ConceptsRepository.distinct(
        f"metadata.{TASK_METADATA_KEY_EXTERNAL_REFERENCES}.{_JIRA_SOURCE_SYSTEM}.external_id",
        query,
    )

    unique_keys = sorted(
        {
            issue_key
            for raw_value in raw_values
            for issue_key in [normalise_jira_issue_key(raw_value)]
            if isinstance(issue_key, str) and issue_key
        }
    )
    if bounded_limit is None:
        return unique_keys
    return unique_keys[:bounded_limit]


_PARITY_CLASS_MUST_FIX = "must_fix"
_PARITY_CLASS_ACCEPTABLE_DEFER = "acceptable_defer"

_PARITY_FIELD_CLASSIFICATION: Dict[str, str] = {
    "status_mapping": _PARITY_CLASS_MUST_FIX,
    "priority_mapping": _PARITY_CLASS_MUST_FIX,
    "components": _PARITY_CLASS_ACCEPTABLE_DEFER,
    "fixVersions": _PARITY_CLASS_ACCEPTABLE_DEFER,
    "sprint": _PARITY_CLASS_ACCEPTABLE_DEFER,
    "rank": _PARITY_CLASS_ACCEPTABLE_DEFER,
}


def _classify_parity_gap(field_name: Any) -> str:
    if not isinstance(field_name, str):
        return _PARITY_CLASS_ACCEPTABLE_DEFER
    return _PARITY_FIELD_CLASSIFICATION.get(
        field_name,
        _PARITY_CLASS_ACCEPTABLE_DEFER,
    )


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

        if follow_up_work:
            projects_with_follow_up += 1
            follow_up_items += len(follow_up_work)

        mapped_fields = [
            "project.identity",
            "status_mapping",
            "priority_mapping",
        ]
        if component_names:
            mapped_fields.append("components")
        if fix_version_names:
            mapped_fields.append("fixVersions")
        if sprint_values:
            mapped_fields.append("sprint")
        if rank_values:
            mapped_fields.append("rank")

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
                "mapped_fields": mapped_fields,
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
                "planning_fields": {
                    "components": component_names,
                    "fix_versions": fix_version_names,
                    "sprint_values": sprint_values,
                    "rank_values": rank_values,
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


def _build_pilot_validation_report(
    *,
    project_parity: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> Dict[str, Any]:
    raw_project_rows = project_parity.get("projects")
    project_rows = raw_project_rows if isinstance(raw_project_rows, list) else []

    must_fix_gaps: list[Dict[str, Any]] = []
    defer_gaps: list[Dict[str, Any]] = []
    for project_row in project_rows:
        if not isinstance(project_row, Mapping):
            continue
        project_key = (
            project_row.get("project_key")
            if isinstance(project_row.get("project_key"), str)
            else None
        )
        dropped_fields_raw = project_row.get("dropped_fields")
        if not isinstance(dropped_fields_raw, list):
            dropped_fields_raw = []
        for dropped_field in dropped_fields_raw:
            if not isinstance(dropped_field, Mapping):
                continue
            field_name = (
                dropped_field.get("field")
                if isinstance(dropped_field.get("field"), str)
                else None
            )
            classification = _classify_parity_gap(field_name)
            gap_row = {
                "project_key": project_key,
                "field": field_name,
                "reason": dropped_field.get("reason"),
                "values": dropped_field.get("values"),
                "classification": classification,
            }
            if classification == _PARITY_CLASS_MUST_FIX:
                must_fix_gaps.append(gap_row)
            else:
                defer_gaps.append(gap_row)

        planning_fields_raw = project_row.get("planning_fields")
        planning_fields: Mapping[str, Any]
        if isinstance(planning_fields_raw, Mapping):
            planning_fields = planning_fields_raw
        else:
            planning_fields = {}
        planning_gap_specs = (
            ("components", planning_fields.get("components")),
            ("fix_versions", planning_fields.get("fix_versions")),
            ("sprint_values", planning_fields.get("sprint_values")),
            ("rank_values", planning_fields.get("rank_values")),
        )
        for field_name, values in planning_gap_specs:
            if not isinstance(values, list) or not values:
                continue
            defer_gaps.append(
                {
                    "project_key": project_key,
                    "field": field_name,
                    "reason": "mapped_to_task_metadata_pending_typed_planning_model",
                    "values": values,
                    "classification": _PARITY_CLASS_ACCEPTABLE_DEFER,
                }
            )

    if must_fix_gaps:
        recommendation = "no_go"
        recommendation_reason = (
            "Must-fix parity gaps remain. Address these before relying on Von as the "
            "primary task system for Jira-style operations."
        )
    elif defer_gaps:
        recommendation = "go_with_conditions"
        recommendation_reason = (
            "Core operational parity is sufficient for Von-first task operations, with "
            "acceptable planning-model gaps deferred."
        )
    else:
        recommendation = "go"
        recommendation_reason = (
            "Parity checks found no blocking or deferred gaps in the evaluated sample."
        )

    return {
        "summary": {
            "issues_scanned": int(summary.get("total_issues", 0)),
            "must_fix_gap_count": len(must_fix_gaps),
            "acceptable_defer_gap_count": len(defer_gaps),
        },
        "recommendation": recommendation,
        "recommendation_reason": recommendation_reason,
        "must_fix_gaps": must_fix_gaps,
        "acceptable_defer_gaps": defer_gaps,
        "operational_surface": {
            "create_update_query": True,
            "hierarchy_links": True,
            "transitions": True,
            "comments_attachments_history": True,
        },
    }


def import_jira_issues_to_tasks(
    *,
    issues: Sequence[Mapping[str, Any]],
    dry_run: bool = True,
    actor_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    assignee_account_id_to_concept_id: Mapping[str, str] | None = None,
    jira_account_id_to_concept_id: Mapping[str, str] | None = None,
    update_existing: bool = True,
    auto_resolve_participants: bool = True,
    create_missing_participant_concepts: bool = True,
) -> Dict[str, Any]:
    """Import Jira issues into Von tasks.

    Args:
        issues: Jira issue payloads (dict-like) from Jira MCP.
        dry_run: Preview-only mode. No writes are performed.
        actor_concept_id: Optional Von concept id of the importing actor.
        organisation_concept_id: Optional organisation scope for created tasks.
        namespace: Optional namespace scope for blob-backed attachment imports.
        assignee_account_id_to_concept_id: Optional map from Jira accountId to
            Von concept ids for assignee linkage (legacy alias for
            jira_account_id_to_concept_id).
        jira_account_id_to_concept_id: Optional map from Jira accountId to Von
            person concept IDs used for assignee/creator/reporter/watchers.
        update_existing: When True, reruns update already-mapped tasks.
        auto_resolve_participants: When True, attempt deterministic participant
            concept resolution from Jira accountId/email/display name.
        create_missing_participant_concepts: When True, create #V#person concepts
            for unresolved Jira participants when dry_run is False.
    """

    participant_map: Dict[str, str] = {}
    raw_maps = []
    if isinstance(assignee_account_id_to_concept_id, Mapping):
        raw_maps.append(assignee_account_id_to_concept_id)
    if isinstance(jira_account_id_to_concept_id, Mapping):
        raw_maps.append(jira_account_id_to_concept_id)
    for raw_map in raw_maps:
        for key, value in raw_map.items():
            account_id = _normalise_jira_account_id(key)
            concept_id = ensure_v_concept_prefix(value)
            if not account_id or not concept_id:
                continue
            participant_map[account_id] = concept_id

    participant_account_cache: Dict[str, str] = {}
    participant_email_cache: Dict[str, str] = {}

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
        "participant_fields_mapped": 0,
        "participant_fields_dropped": 0,
        "participant_concepts_created": 0,
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

        assignee_raw = fields.get("assignee")
        creator_raw = fields.get("creator")
        reporter_raw = fields.get("reporter")
        watcher_participants, watcher_count = _extract_jira_watcher_participants(
            raw_issue,
            fields,
        )

        assignee_resolution = _ensure_jira_participant_concept(
            raw_participant=assignee_raw if isinstance(assignee_raw, Mapping) else None,
            account_id_to_concept_id=participant_map,
            account_cache=participant_account_cache,
            email_cache=participant_email_cache,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            allow_lookup=bool(auto_resolve_participants),
            allow_create=(
                bool(auto_resolve_participants)
                and bool(create_missing_participant_concepts)
                and not dry_run
            ),
        )
        creator_resolution = _ensure_jira_participant_concept(
            raw_participant=creator_raw if isinstance(creator_raw, Mapping) else None,
            account_id_to_concept_id=participant_map,
            account_cache=participant_account_cache,
            email_cache=participant_email_cache,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            allow_lookup=bool(auto_resolve_participants),
            allow_create=(
                bool(auto_resolve_participants)
                and bool(create_missing_participant_concepts)
                and not dry_run
            ),
        )
        reporter_resolution = _ensure_jira_participant_concept(
            raw_participant=reporter_raw if isinstance(reporter_raw, Mapping) else None,
            account_id_to_concept_id=participant_map,
            account_cache=participant_account_cache,
            email_cache=participant_email_cache,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            allow_lookup=bool(auto_resolve_participants),
            allow_create=(
                bool(auto_resolve_participants)
                and bool(create_missing_participant_concepts)
                and not dry_run
            ),
        )

        assignee_concept_id = (
            assignee_resolution.get("concept_id")
            if isinstance(assignee_resolution.get("concept_id"), str)
            else None
        )
        creator_concept_id = (
            creator_resolution.get("concept_id")
            if isinstance(creator_resolution.get("concept_id"), str)
            else None
        )
        reporter_concept_id = (
            reporter_resolution.get("concept_id")
            if isinstance(reporter_resolution.get("concept_id"), str)
            else None
        )

        watcher_resolution_rows: list[Dict[str, Any]] = []
        watcher_concept_ids: list[str] = []
        for watcher_raw in watcher_participants:
            watcher_resolution = _ensure_jira_participant_concept(
                raw_participant=watcher_raw,
                account_id_to_concept_id=participant_map,
                account_cache=participant_account_cache,
                email_cache=participant_email_cache,
                actor_concept_id=actor_concept_id,
                organisation_concept_id=organisation_concept_id,
                allow_lookup=bool(auto_resolve_participants),
                allow_create=(
                    bool(auto_resolve_participants)
                    and bool(create_missing_participant_concepts)
                    and not dry_run
                ),
            )
            watcher_resolution_rows.append(watcher_resolution)
            concept_id = watcher_resolution.get("concept_id")
            if isinstance(concept_id, str) and concept_id and concept_id not in watcher_concept_ids:
                watcher_concept_ids.append(concept_id)

        participant_resolution = {
            "assignee": assignee_resolution,
            "creator": creator_resolution,
            "reporter": reporter_resolution,
            "watchers": watcher_resolution_rows,
        }

        for role_name, concept_id, missing_reason in (
            ("assignee", assignee_concept_id, "jira_assignee_mapping_missing"),
            ("creator", creator_concept_id, "jira_creator_mapping_missing"),
            ("reporter", reporter_concept_id, "jira_reporter_mapping_missing"),
        ):
            role_source = participant_resolution.get(role_name)
            role_participant = (
                role_source.get("participant") if isinstance(role_source, Mapping) else None
            )
            if isinstance(concept_id, str) and concept_id:
                mapped_fields.append(role_name)
                summary["participant_fields_mapped"] = int(
                    summary.get("participant_fields_mapped", 0)
                ) + 1
                if isinstance(role_source, Mapping) and role_source.get("created_concept"):
                    summary["participant_concepts_created"] = int(
                        summary.get("participant_concepts_created", 0)
                    ) + 1
            elif isinstance(role_participant, Mapping):
                dropped_fields.append({"field": role_name, "reason": missing_reason})
                summary["participant_fields_dropped"] = int(
                    summary.get("participant_fields_dropped", 0)
                ) + 1

        if watcher_participants:
            if watcher_concept_ids:
                mapped_fields.append("watchers")
                summary["participant_fields_mapped"] = int(
                    summary.get("participant_fields_mapped", 0)
                ) + len(watcher_concept_ids)
                summary["participant_concepts_created"] = int(
                    summary.get("participant_concepts_created", 0)
                ) + sum(
                    1
                    for row in watcher_resolution_rows
                    if isinstance(row, Mapping) and row.get("created_concept")
                )
            else:
                dropped_fields.append(
                    {
                        "field": "watchers",
                        "reason": "jira_watcher_mapping_missing",
                    }
                )
                summary["participant_fields_dropped"] = int(
                    summary.get("participant_fields_dropped", 0)
                ) + len(watcher_participants)

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
            mapped_fields.append("components")

        fix_version_names = _extract_named_values(fields.get("fixVersions"))
        if fix_version_names:
            project_observation["fix_version_names"].update(fix_version_names)
            mapped_fields.append("fix_versions")

        sprint_values = _extract_sprint_values(fields)
        if sprint_values:
            project_observation["sprint_values"].update(sprint_values)
            mapped_fields.append("sprint_values")

        rank_value = _extract_rank_value(fields)
        if rank_value:
            project_observation["rank_values"].add(rank_value)
            mapped_fields.append("backlog_rank")

        status_history, status_history_warning = _extract_status_history(raw_issue)
        if status_history_warning:
            dropped_fields.append(
                {"field": "status_history", "reason": status_history_warning}
            )
        elif status_history:
            mapped_fields.append("status_history")

        existing_task = find_task_by_external_reference(
            source_system=_JIRA_SOURCE_SYSTEM,
            external_id=issue_key,
            organisation_concept_id=organisation_concept_id,
        )
        existing_task_id = (
            existing_task.get("task_concept_id")
            if isinstance(existing_task, Mapping)
            and isinstance(existing_task.get("task_concept_id"), str)
            else _resolve_existing_task_id(
                jira_issue_key=issue_key,
                organisation_concept_id=organisation_concept_id,
                cache=existing_cache,
            )
        )
        existing_task_id_str = (
            existing_task_id.strip()
            if isinstance(existing_task_id, str) and existing_task_id.strip()
            else None
        )

        task_id: str | None = None
        action: str

        update_fields_payload: Dict[str, Any] = {
            "status": mapped_status,
            "priority": mapped_priority,
            "labels": labels,
            "task_source_id": JIRA_IMPORTED_TASK_SOURCE_ID,
            "reference_code": issue_key,
        }
        if organisation_concept_id is not None:
            update_fields_payload["organisation_concept_id"] = organisation_concept_id
        if due_date is not None:
            update_fields_payload["due_date"] = due_date
        if start_date is not None:
            update_fields_payload["start_date"] = start_date
        if assignee_concept_id is not None:
            update_fields_payload["assignee_concept_id"] = assignee_concept_id
        if creator_concept_id is not None:
            update_fields_payload["created_by_concept_id"] = creator_concept_id
        if reporter_concept_id is not None:
            update_fields_payload["reporter_concept_id"] = reporter_concept_id
        if watcher_concept_ids:
            update_fields_payload["watcher_concept_ids"] = watcher_concept_ids
        if component_names:
            update_fields_payload["components"] = component_names
        if fix_version_names:
            update_fields_payload["fix_versions"] = fix_version_names
        if sprint_values:
            update_fields_payload["sprint_values"] = sprint_values
        if rank_value:
            update_fields_payload["backlog_rank"] = rank_value

        try:
            if existing_task_id_str is not None:
                task_id = existing_task_id_str
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
                        created_by_concept_id=creator_concept_id or actor_concept_id,
                        organisation_concept_id=organisation_concept_id,
                        task_source_id=JIRA_IMPORTED_TASK_SOURCE_ID,
                        reference_code=issue_key,
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
                    action = "created"
                    summary["created"] += 1
                    existing_cache[issue_key] = task_id

            activity_checkpoint = _existing_imported_activity_checkpoint(existing_task)
            if isinstance(task_id, str) and task_id:
                imported_activity, mapped_activity_fields, dropped_activity_fields = (
                    _materialise_jira_issue_activity(
                        task_concept_id=task_id,
                        issue_key=issue_key,
                        raw_issue=raw_issue,
                        actor_concept_id=actor_concept_id,
                        organisation_concept_id=organisation_concept_id,
                        namespace=namespace,
                        participant_map=participant_map,
                        participant_account_cache=participant_account_cache,
                        participant_email_cache=participant_email_cache,
                        auto_resolve_participants=bool(auto_resolve_participants),
                        create_missing_participant_concepts=bool(
                            create_missing_participant_concepts
                        ),
                        dry_run=dry_run,
                        existing_activity_checkpoint=activity_checkpoint,
                    )
                )
                for field_name in mapped_activity_fields:
                    if field_name not in mapped_fields:
                        mapped_fields.append(field_name)
                dropped_fields.extend(dropped_activity_fields)
            else:
                imported_activity = {
                    key: sorted(values) for key, values in activity_checkpoint.items()
                }

            if not dry_run and isinstance(task_id, str) and task_id and not task_id.startswith("planned:"):
                reference_payload = _issue_reference_payload(
                    raw_issue,
                    issue_key=issue_key,
                    labels=labels,
                    component_names=component_names,
                    fix_version_names=fix_version_names,
                    sprint_values=sprint_values,
                    rank_value=rank_value,
                    status_history=status_history,
                    parent_issue_key=parent_issue_key,
                    epic_issue_key=epic_issue_key,
                    link_summaries=issue_links,
                    assignee=(
                        assignee_resolution.get("participant")
                        if isinstance(assignee_resolution, Mapping)
                        else None
                    ),
                    creator=(
                        creator_resolution.get("participant")
                        if isinstance(creator_resolution, Mapping)
                        else None
                    ),
                    reporter=(
                        reporter_resolution.get("participant")
                        if isinstance(reporter_resolution, Mapping)
                        else None
                    ),
                    watcher_participants=watcher_participants,
                    watcher_count=watcher_count,
                    assignee_concept_id=assignee_concept_id,
                    creator_concept_id=creator_concept_id,
                    reporter_concept_id=reporter_concept_id,
                    watcher_concept_ids=watcher_concept_ids,
                )
                reference_payload["imported_activity"] = imported_activity
                upsert_task_external_reference(
                    task_id,
                    source_system=_JIRA_SOURCE_SYSTEM,
                    external_id=issue_key,
                    reference_payload=reference_payload,
                    actor_concept_id=actor_concept_id,
                )
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
                "participant_mappings": participant_resolution,
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

    project_parity_report = _build_project_parity_report(project_observations)
    pilot_validation = _build_pilot_validation_report(
        project_parity=project_parity_report,
        summary=summary,
    )

    return {
        "success": True,
        "source_system": _JIRA_SOURCE_SYSTEM,
        "dry_run": bool(dry_run),
        "update_existing": bool(update_existing),
        "summary": summary,
        "issues": issue_results,
        "project_parity": project_parity_report,
        "pilot_validation": pilot_validation,
    }
