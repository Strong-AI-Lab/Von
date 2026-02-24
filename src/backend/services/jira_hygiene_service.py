"""Workflow helper logic for Jira hygiene and orphan epic triage.

This module keeps Jira-hygiene behaviour deterministic and tool-oriented so
workflow orchestration can live in Vontology process graphs instead of bespoke
Python control flow. See JVNAUTOSCI-929 and JVNAUTOSCI-930.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

_WORD_RE = re.compile(r"[a-z0-9]+")

_DEFAULT_PROJECT_KEY = "JVNAUTOSCI"
_DEFAULT_MAX_ISSUES = 200
_DEFAULT_MAX_EPICS = 120
_DEFAULT_BATCH_SIZE = 10
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BACKOFF_SECONDS = 1.0

_MAX_BATCH_SIZE = 100
_MAX_RETRIES = 8
_MAX_RETRY_BACKOFF_SECONDS = 8.0

_TRANSIENT_ERROR_MARKERS: tuple[str, ...] = (
    "429",
    "rate",
    "too many requests",
    "timeout",
    "timed out",
    "temporar",
    "service unavailable",
    "gateway timeout",
    "bad gateway",
    "502",
    "503",
    "504",
)

_DONE_STATUS_NAMES: set[str] = {
    "done",
    "closed",
    "resolved",
    "superseded",
    "won't fix",
    "wont fix",
}

_IN_PROGRESS_STALE_DAYS = 14

_IN_PROGRESS_RECOMMENDATION_CANDIDATE_DONE = "candidate_done"
_IN_PROGRESS_RECOMMENDATION_CANDIDATE_TODO = "candidate_todo"
_IN_PROGRESS_RECOMMENDATION_KEEP = "keep_in_progress"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_jira_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        # Jira often returns offsets like +1300, but datetime.fromisoformat expects +13:00.
        if len(text) > 5 and (text[-5] in {"+", "-"}) and text[-3] != ":":
            text = f"{text[:-2]}:{text[-2:]}"
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _status_category_name(raw_status: Any) -> str | None:
    if not isinstance(raw_status, Mapping):
        return None
    raw_category = raw_status.get("statusCategory")
    if isinstance(raw_category, Mapping):
        name = raw_category.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _status_is_done(raw_status: Any) -> bool:
    if not isinstance(raw_status, Mapping):
        if isinstance(raw_status, str):
            return raw_status.strip().lower() in _DONE_STATUS_NAMES
        return False
    category_name = _status_category_name(raw_status)
    if isinstance(category_name, str) and category_name.strip().lower() == "done":
        return True
    status_name = raw_status.get("name")
    if isinstance(status_name, str) and status_name.strip():
        return status_name.strip().lower() in _DONE_STATUS_NAMES
    return False


def _extract_subtask_progress(fields: Mapping[str, Any]) -> tuple[int, int, int]:
    raw_subtasks = fields.get("subtasks")
    if not isinstance(raw_subtasks, list):
        return (0, 0, 0)
    total = 0
    done = 0
    for subtask in raw_subtasks:
        if not isinstance(subtask, Mapping):
            continue
        total += 1
        subtask_fields = _extract_issue_fields(subtask)
        if _status_is_done(subtask_fields.get("status")):
            done += 1
    open_count = max(0, total - done)
    return (total, done, open_count)


def _extract_linked_issue_progress(fields: Mapping[str, Any]) -> tuple[int, int]:
    raw_links = fields.get("issuelinks")
    if not isinstance(raw_links, list):
        return (0, 0)

    done_count = 0
    not_done_count = 0
    for link in raw_links:
        if not isinstance(link, Mapping):
            continue
        for side in ("inwardIssue", "outwardIssue"):
            linked_issue = link.get(side)
            if not isinstance(linked_issue, Mapping):
                continue
            linked_fields = _extract_issue_fields(linked_issue)
            raw_status = linked_fields.get("status")
            if not raw_status:
                continue
            if _status_is_done(raw_status):
                done_count += 1
            else:
                not_done_count += 1
    return (done_count, not_done_count)


def _days_since(updated_at: Any, *, now_utc: datetime) -> int | None:
    parsed = _parse_jira_datetime(updated_at)
    if parsed is None:
        return None
    return max(0, int((now_utc - parsed.astimezone(timezone.utc)).days))


def _build_in_progress_review_record(
    issue_doc: Mapping[str, Any],
    *,
    now_utc: datetime,
) -> dict[str, Any] | None:
    issue_record = _extract_issue_record(issue_doc)
    if issue_record is None:
        return None

    issue_type_name = issue_record.get("issue_type")
    issue_type_normalised = str(issue_type_name or "").strip().lower()
    if issue_type_normalised == "epic" or _is_subtask_issue(issue_type_name):
        return None

    fields = _extract_issue_fields(issue_doc)
    if not isinstance(fields.get("status"), Mapping):
        return None

    subtasks_total, subtasks_done, subtasks_open = _extract_subtask_progress(fields)
    linked_done_count, linked_not_done_count = _extract_linked_issue_progress(fields)
    days_since_update = _days_since(fields.get("updated"), now_utc=now_utc)

    recommended_action = _IN_PROGRESS_RECOMMENDATION_KEEP
    reason = "active_scope_detected"

    if subtasks_total > 0 and subtasks_open == 0:
        recommended_action = _IN_PROGRESS_RECOMMENDATION_CANDIDATE_DONE
        reason = "all_subtasks_done"
    elif (
        subtasks_total == 0
        and linked_not_done_count == 0
        and days_since_update is not None
        and days_since_update >= _IN_PROGRESS_STALE_DAYS
    ):
        recommended_action = _IN_PROGRESS_RECOMMENDATION_CANDIDATE_TODO
        reason = "stale_without_active_children"

    return {
        "issue_key": issue_record["issue_key"],
        "summary": issue_record.get("summary"),
        "issue_type": issue_record.get("issue_type"),
        "status": issue_record.get("status"),
        "parent_issue_key": issue_record.get("parent_issue_key"),
        "updated": fields.get("updated"),
        "status_category_changed": fields.get("statuscategorychangedate"),
        "days_since_update": days_since_update,
        "subtasks_total": subtasks_total,
        "subtasks_done": subtasks_done,
        "subtasks_open": subtasks_open,
        "linked_done_count": linked_done_count,
        "linked_not_done_count": linked_not_done_count,
        "recommended_action": recommended_action,
        "recommended_action_reason": reason,
    }


def _coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _normalise_issue_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def _normalise_issue_key_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    keys: set[str] = set()
    for item in value:
        cleaned = _normalise_issue_key(item)
        if cleaned:
            keys.add(cleaned)
    return keys


def _extract_plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return ""
    if value.get("type") == "text":
        text_value = value.get("text")
        return text_value.strip() if isinstance(text_value, str) else ""
    raw_children = value.get("content")
    if not isinstance(raw_children, list):
        return ""
    chunks: list[str] = []
    for child in raw_children:
        text = _extract_plain_text(child)
        if text:
            chunks.append(text)
    return " ".join(chunks).strip()


def _extract_issue_fields(issue_doc: Mapping[str, Any]) -> Mapping[str, Any]:
    raw_fields = issue_doc.get("fields")
    if isinstance(raw_fields, Mapping):
        return raw_fields
    return {}


def _extract_status_name(fields: Mapping[str, Any]) -> str | None:
    raw_status = fields.get("status")
    if isinstance(raw_status, Mapping):
        name = raw_status.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(raw_status, str) and raw_status.strip():
        return raw_status.strip()
    return None


def _extract_issue_type_name(fields: Mapping[str, Any]) -> str | None:
    raw_issue_type = fields.get("issuetype")
    if isinstance(raw_issue_type, Mapping):
        name = raw_issue_type.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        if isinstance(raw_issue_type.get("subtask"), bool) and raw_issue_type.get(
            "subtask"
        ):
            return "Sub-task"
    if isinstance(raw_issue_type, str) and raw_issue_type.strip():
        return raw_issue_type.strip()
    return None


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


def _normalise_str_list(raw_items: Any) -> list[str]:
    if not isinstance(raw_items, list):
        return []
    seen: set[str] = set()
    values: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            candidate = item.strip()
        elif isinstance(item, Mapping):
            raw_name = item.get("name")
            candidate = raw_name.strip() if isinstance(raw_name, str) else ""
        else:
            candidate = ""
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        values.append(candidate)
    return values


def _tokenise(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if isinstance(value, list):
            for item in value:
                tokens.update(_tokenise(item))
            continue
        if not isinstance(value, str):
            continue
        for token in _WORD_RE.findall(value.lower()):
            if len(token) < 3:
                continue
            tokens.add(token)
    return tokens


def _extract_issues(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_issues = payload.get("issues")
    if not isinstance(raw_issues, list):
        return []
    return [dict(item) for item in raw_issues if isinstance(item, Mapping)]


def _extract_issue_record(issue_doc: Mapping[str, Any]) -> dict[str, Any] | None:
    issue_key = _normalise_issue_key(issue_doc.get("key"))
    if not issue_key:
        return None

    fields = _extract_issue_fields(issue_doc)
    issue_type = _extract_issue_type_name(fields)
    status_name = _extract_status_name(fields)
    parent_issue_key = _extract_parent_issue_key(fields)
    epic_issue_key = _extract_epic_issue_key(fields)
    labels = _normalise_str_list(fields.get("labels"))
    components = _normalise_str_list(fields.get("components"))
    description_text = _extract_plain_text(fields.get("description"))

    return {
        "issue_key": issue_key,
        "summary": str(fields.get("summary") or issue_doc.get("summary") or "").strip(),
        "issue_type": issue_type,
        "status": status_name,
        "parent_issue_key": parent_issue_key,
        "current_epic_key": parent_issue_key or epic_issue_key,
        "labels": labels,
        "components": components,
        "description_excerpt": description_text[:240] if description_text else "",
        "tokens": sorted(
            _tokenise(
                fields.get("summary"),
                labels,
                components,
                description_text[:480],
            )
        ),
    }


def _extract_epic_record(issue_doc: Mapping[str, Any]) -> dict[str, Any] | None:
    issue_record = _extract_issue_record(issue_doc)
    if issue_record is None:
        return None
    issue_type = str(issue_record.get("issue_type") or "").strip().lower()
    if issue_type != "epic":
        return None
    return {
        "epic_key": issue_record["issue_key"],
        "summary": issue_record["summary"],
        "status": issue_record["status"],
        "labels": issue_record["labels"],
        "components": issue_record["components"],
        "tokens": issue_record["tokens"],
    }


def _is_subtask_issue(issue_type_name: str | None) -> bool:
    issue_type = str(issue_type_name or "").strip().lower()
    return issue_type in {"sub-task", "subtask", "sub task"}


def _is_true_orphan(issue: Mapping[str, Any]) -> bool:
    issue_type = str(issue.get("issue_type") or "").strip().lower()
    if issue_type == "epic" or _is_subtask_issue(issue.get("issue_type")):
        return False
    if issue.get("parent_issue_key"):
        return False
    if issue.get("current_epic_key"):
        return False
    return True


def _epic_match_scores(
    issue_record: Mapping[str, Any],
    epic_catalogue: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    issue_tokens = set(_tokenise(issue_record.get("summary"), issue_record.get("tokens")))
    issue_text = str(issue_record.get("summary") or "").lower()
    if isinstance(issue_record.get("description_excerpt"), str):
        issue_text += " " + str(issue_record.get("description_excerpt")).lower()

    scores: list[dict[str, Any]] = []
    for epic in epic_catalogue:
        epic_key = _normalise_issue_key(epic.get("epic_key"))
        if not epic_key:
            continue
        epic_tokens = set(_tokenise(epic.get("summary"), epic.get("tokens")))
        overlap = issue_tokens.intersection(epic_tokens)
        score = len(overlap)
        if epic_key.lower() in issue_text:
            score += 3
        if isinstance(epic.get("summary"), str):
            summary = epic["summary"].lower()
            if summary and summary in issue_text:
                score += 2
        scores.append(
            {
                "epic_key": epic_key,
                "score": score,
                "overlap_tokens": sorted(overlap),
            }
        )
    scores.sort(
        key=lambda row: (int(row.get("score", 0)), row.get("epic_key")),
        reverse=True,
    )
    return scores


def discover_jira_hygiene(
    *,
    search_issues: Callable[..., Mapping[str, Any]],
    project_key: str | None = None,
    candidate_epic_keys: Sequence[str] | None = None,
    max_issues: int | None = None,
    max_epics: int | None = None,
    include_cross_cutting: bool = True,
    include_in_progress_candidates: bool = True,
) -> dict[str, Any]:
    """Discover epic catalogue and Jira hygiene candidates."""
    project_key_norm = str(project_key or _DEFAULT_PROJECT_KEY).strip().upper()
    issue_limit = _coerce_int(
        max_issues,
        default=_DEFAULT_MAX_ISSUES,
        minimum=1,
        maximum=1000,
    )
    epic_limit = _coerce_int(
        max_epics,
        default=_DEFAULT_MAX_EPICS,
        minimum=1,
        maximum=1000,
    )

    epic_jql = (
        f"project = {project_key_norm} "
        "AND issuetype = Epic "
        "ORDER BY created DESC"
    )
    candidate_jql = (
        f"project = {project_key_norm} "
        "AND issuetype != Epic "
        "AND issuetype != Sub-task "
        "ORDER BY created DESC"
    )
    in_progress_jql = (
        f"project = {project_key_norm} "
        'AND statusCategory = "In Progress" '
        "AND issuetype != Epic "
        "ORDER BY updated DESC"
    )

    epic_response = search_issues(
        jql=epic_jql,
        max_results=epic_limit,
        fields=["summary", "status", "labels", "components", "issuetype"],
    )
    if isinstance(epic_response, Mapping) and epic_response.get("success") is False:
        return {
            "success": False,
            "error": "epic_discovery_failed",
            "error_details": dict(epic_response),
        }

    candidate_response = search_issues(
        jql=candidate_jql,
        max_results=issue_limit,
        fields=[
            "summary",
            "description",
            "status",
            "issuetype",
            "labels",
            "components",
            "parent",
            "customfield_10014",
            "customfield_10008",
            "epic",
            "epic_link",
            "epicLink",
        ],
    )
    if isinstance(candidate_response, Mapping) and candidate_response.get("success") is False:
        return {
            "success": False,
            "error": "candidate_discovery_failed",
            "error_details": dict(candidate_response),
        }

    in_progress_response: Mapping[str, Any] | None = None
    if include_in_progress_candidates:
        in_progress_response = search_issues(
            jql=in_progress_jql,
            max_results=issue_limit,
            fields=[
                "summary",
                "status",
                "statuscategorychangedate",
                "updated",
                "issuetype",
                "parent",
                "subtasks",
                "issuelinks",
                "customfield_10014",
                "customfield_10008",
                "epic",
                "epic_link",
                "epicLink",
            ],
        )
        if (
            isinstance(in_progress_response, Mapping)
            and in_progress_response.get("success") is False
        ):
            return {
                "success": False,
                "error": "in_progress_discovery_failed",
                "error_details": dict(in_progress_response),
            }

    raw_epics = _extract_issues(epic_response if isinstance(epic_response, Mapping) else {})
    raw_candidates = _extract_issues(
        candidate_response if isinstance(candidate_response, Mapping) else {}
    )
    raw_in_progress = _extract_issues(
        in_progress_response if isinstance(in_progress_response, Mapping) else {}
    )

    epic_catalogue: list[dict[str, Any]] = []
    for raw_epic in raw_epics:
        epic_record = _extract_epic_record(raw_epic)
        if epic_record is not None:
            epic_catalogue.append(epic_record)

    requested_epic_keys = {
        key
        for key in (
            _normalise_issue_key(item)
            for item in (candidate_epic_keys or [])
        )
        if key
    }
    missing_requested_epic_keys: list[str] = []
    if requested_epic_keys:
        available_epic_keys = {
            str(item.get("epic_key"))
            for item in epic_catalogue
            if isinstance(item.get("epic_key"), str)
        }
        missing_requested_epic_keys = sorted(requested_epic_keys - available_epic_keys)
        epic_catalogue = [
            epic
            for epic in epic_catalogue
            if isinstance(epic.get("epic_key"), str)
            and epic["epic_key"] in requested_epic_keys
        ]

    orphan_candidates: list[dict[str, Any]] = []
    cross_cutting_candidates: list[dict[str, Any]] = []
    for raw_issue in raw_candidates:
        issue_record = _extract_issue_record(raw_issue)
        if issue_record is None:
            continue
        if _is_true_orphan(issue_record):
            orphan_candidates.append(issue_record)
            continue
        if include_cross_cutting and issue_record.get("current_epic_key"):
            cross_cutting_candidates.append(issue_record)

    in_progress_candidates: list[dict[str, Any]] = []
    if include_in_progress_candidates:
        now_utc = datetime.now(timezone.utc)
        for raw_issue in raw_in_progress:
            review_record = _build_in_progress_review_record(raw_issue, now_utc=now_utc)
            if review_record is None:
                continue
            in_progress_candidates.append(review_record)

    in_progress_review_summary = {
        _IN_PROGRESS_RECOMMENDATION_CANDIDATE_DONE: 0,
        _IN_PROGRESS_RECOMMENDATION_CANDIDATE_TODO: 0,
        _IN_PROGRESS_RECOMMENDATION_KEEP: 0,
    }
    for item in in_progress_candidates:
        recommendation = str(item.get("recommended_action") or "").strip().lower()
        if recommendation in in_progress_review_summary:
            in_progress_review_summary[recommendation] += 1

    return {
        "success": True,
        "project_key": project_key_norm,
        "epic_catalogue": epic_catalogue,
        "orphan_candidates": orphan_candidates,
        "cross_cutting_candidates": cross_cutting_candidates,
        "in_progress_candidates": in_progress_candidates,
        "in_progress_review_summary": in_progress_review_summary,
        "discovery_counts": {
            "epics": len(epic_catalogue),
            "orphans": len(orphan_candidates),
            "cross_cutting": len(cross_cutting_candidates),
            "in_progress_candidates": len(in_progress_candidates),
            "candidate_issues_scanned": len(raw_candidates),
        },
        "missing_requested_epic_keys": missing_requested_epic_keys,
        "discovery_jql": {
            "epics": epic_jql,
            "candidates": candidate_jql,
            "in_progress_candidates": (
                in_progress_jql if include_in_progress_candidates else None
            ),
        },
        "include_in_progress_candidates": bool(include_in_progress_candidates),
        "discovered_at_utc": _utc_now_iso(),
    }


def propose_jira_hygiene_plan(
    *,
    epic_catalogue: Sequence[Mapping[str, Any]] | None,
    orphan_candidates: Sequence[Mapping[str, Any]] | None,
    cross_cutting_candidates: Sequence[Mapping[str, Any]] | None = None,
    in_progress_candidates: Sequence[Mapping[str, Any]] | None = None,
    batch_size: int | None = None,
) -> dict[str, Any]:
    """Build a deterministic dry-run proposal for Jira hygiene actions."""
    epics = [dict(item) for item in (epic_catalogue or []) if isinstance(item, Mapping)]
    orphans = [dict(item) for item in (orphan_candidates or []) if isinstance(item, Mapping)]
    cross_cutting = [
        dict(item) for item in (cross_cutting_candidates or []) if isinstance(item, Mapping)
    ]
    in_progress = [
        dict(item) for item in (in_progress_candidates or []) if isinstance(item, Mapping)
    ]

    ready_to_execute: list[dict[str, Any]] = []
    needs_decision: list[dict[str, Any]] = []
    no_action: list[dict[str, Any]] = []
    grouped_counts: dict[str, dict[str, int]] = {}
    in_progress_review_counts = {
        _IN_PROGRESS_RECOMMENDATION_CANDIDATE_DONE: 0,
        _IN_PROGRESS_RECOMMENDATION_CANDIDATE_TODO: 0,
        _IN_PROGRESS_RECOMMENDATION_KEEP: 0,
    }

    for issue in orphans:
        issue_key = _normalise_issue_key(issue.get("issue_key"))
        if not issue_key:
            continue

        epic_scores = _epic_match_scores(issue, epics)
        best = epic_scores[0] if epic_scores else None
        second = epic_scores[1] if len(epic_scores) > 1 else None
        best_score = int(best.get("score", 0)) if isinstance(best, Mapping) else 0
        second_score = int(second.get("score", 0)) if isinstance(second, Mapping) else 0

        if not best or best_score <= 0:
            needs_decision.append(
                {
                    "issue_key": issue_key,
                    "summary": issue.get("summary"),
                    "reason": "no_epic_match",
                    "candidate_epic_scores": epic_scores[:3],
                }
            )
            continue

        if second and second_score > 0 and (best_score - second_score) <= 1:
            needs_decision.append(
                {
                    "issue_key": issue_key,
                    "summary": issue.get("summary"),
                    "reason": "ambiguous_match",
                    "candidate_epic_scores": epic_scores[:3],
                }
            )
            continue

        target_epic_key = _normalise_issue_key(best.get("epic_key"))
        if not target_epic_key:
            continue
        ready_to_execute.append(
            {
                "operation_id": f"assign-{issue_key}",
                "issue_key": issue_key,
                "operation": "assign_epic",
                "target_epic_key": target_epic_key,
                "rationale": (
                    "keyword_overlap:"
                    + ",".join(best.get("overlap_tokens", [])[:8])
                ),
                "issue_summary": issue.get("summary"),
            }
        )
        bucket = grouped_counts.setdefault(
            target_epic_key, {"assign_epic": 0, "add_comment": 0}
        )
        bucket["assign_epic"] += 1

    for issue in cross_cutting:
        issue_key = _normalise_issue_key(issue.get("issue_key"))
        current_epic_key = _normalise_issue_key(issue.get("current_epic_key"))
        if not issue_key or not current_epic_key:
            continue

        epic_scores = [
            row
            for row in _epic_match_scores(issue, epics)
            if _normalise_issue_key(row.get("epic_key")) != current_epic_key
        ]
        best_alt = epic_scores[0] if epic_scores else None
        if not isinstance(best_alt, Mapping) or int(best_alt.get("score", 0)) <= 0:
            no_action.append(
                {
                    "issue_key": issue_key,
                    "summary": issue.get("summary"),
                    "reason": "already_well_placed",
                }
            )
            continue

        related_epic_key = _normalise_issue_key(best_alt.get("epic_key"))
        if not related_epic_key:
            continue
        ready_to_execute.append(
            {
                "operation_id": f"comment-{issue_key}",
                "issue_key": issue_key,
                "operation": "add_comment",
                "related_epic_key": related_epic_key,
                "rationale": (
                    "cross_cutting_keyword_overlap:"
                    + ",".join(best_alt.get("overlap_tokens", [])[:8])
                ),
                "comment_text": (
                    f"Cross-cutting note (auto-generated): this issue is also relevant to "
                    f"{related_epic_key}. Keeping current epic placement for roadmap stability."
                ),
            }
        )
        bucket = grouped_counts.setdefault(
            related_epic_key, {"assign_epic": 0, "add_comment": 0}
        )
        bucket["add_comment"] += 1

    for issue in in_progress:
        issue_key = _normalise_issue_key(issue.get("issue_key"))
        if not issue_key:
            continue
        recommended_action = str(issue.get("recommended_action") or "").strip().lower()
        if recommended_action not in in_progress_review_counts:
            recommended_action = _IN_PROGRESS_RECOMMENDATION_KEEP
        in_progress_review_counts[recommended_action] += 1

        if recommended_action in {
            _IN_PROGRESS_RECOMMENDATION_CANDIDATE_DONE,
            _IN_PROGRESS_RECOMMENDATION_CANDIDATE_TODO,
        }:
            needs_decision.append(
                {
                    "issue_key": issue_key,
                    "summary": issue.get("summary"),
                    "reason": f"in_progress_review_{recommended_action}",
                    "recommended_action": recommended_action,
                    "recommended_action_reason": issue.get("recommended_action_reason"),
                    "days_since_update": issue.get("days_since_update"),
                    "subtasks_total": issue.get("subtasks_total"),
                    "subtasks_done": issue.get("subtasks_done"),
                    "subtasks_open": issue.get("subtasks_open"),
                    "linked_done_count": issue.get("linked_done_count"),
                    "linked_not_done_count": issue.get("linked_not_done_count"),
                }
            )
        else:
            no_action.append(
                {
                    "issue_key": issue_key,
                    "summary": issue.get("summary"),
                    "reason": "in_progress_review_keep",
                }
            )

    proposed_batch_size = _coerce_int(
        batch_size,
        default=_DEFAULT_BATCH_SIZE,
        minimum=1,
        maximum=_MAX_BATCH_SIZE,
    )
    expected_batches = (
        (len(ready_to_execute) + proposed_batch_size - 1) // proposed_batch_size
        if ready_to_execute
        else 0
    )

    proposal_summary = {
        "ready_to_execute_count": len(ready_to_execute),
        "needs_decision_count": len(needs_decision),
        "no_action_count": len(no_action),
        "grouped_counts_by_epic": grouped_counts,
        "in_progress_review_counts": in_progress_review_counts,
    }

    return {
        "success": True,
        "ready_to_execute": ready_to_execute,
        "needs_decision": needs_decision,
        "no_action": no_action,
        "proposal_summary": proposal_summary,
        "execution_plan": {
            "batch_size": proposed_batch_size,
            "expected_batches": expected_batches,
            "expected_operations": len(ready_to_execute),
        },
        "approval_required": True,
        "proposed_at_utc": _utc_now_iso(),
    }


def check_jira_hygiene_approval(
    *,
    ready_to_execute: Sequence[Mapping[str, Any]] | None,
    execution_mode: str | None,
    approved: Any,
    excluded_issue_keys: Sequence[str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    batch_size: int | None = None,
) -> dict[str, Any]:
    """Resolve approval-gate decisions and compute approved operation set."""
    operations = [
        dict(item) for item in (ready_to_execute or []) if isinstance(item, Mapping)
    ]
    mode = str(execution_mode or "dry_run").strip().lower()
    mode = "execute" if mode == "execute" else "dry_run"
    approved_flag = _coerce_bool(approved, default=False)

    excluded = _normalise_issue_key_set(list(excluded_issue_keys or []))
    override_map = dict(overrides or {}) if isinstance(overrides, Mapping) else {}

    filtered: list[dict[str, Any]] = []
    excluded_applied: list[str] = []
    overrides_applied: list[str] = []

    for op in operations:
        issue_key = _normalise_issue_key(op.get("issue_key"))
        if issue_key and issue_key in excluded:
            excluded_applied.append(issue_key)
            continue

        updated = dict(op)
        override_value = override_map.get(issue_key) if issue_key else None
        if isinstance(override_value, Mapping):
            if _coerce_bool(override_value.get("exclude"), default=False):
                if issue_key:
                    excluded_applied.append(issue_key)
                continue
            target_epic = _normalise_issue_key(override_value.get("target_epic_key"))
            comment_text = override_value.get("comment_text")
            if target_epic and updated.get("operation") == "assign_epic":
                updated["target_epic_key"] = target_epic
                overrides_applied.append(issue_key or updated.get("operation_id", ""))
            if isinstance(comment_text, str) and comment_text.strip():
                updated["comment_text"] = comment_text.strip()
                overrides_applied.append(issue_key or updated.get("operation_id", ""))
        elif isinstance(override_value, str) and override_value.strip():
            override_epic = _normalise_issue_key(override_value)
            if override_epic and updated.get("operation") == "assign_epic":
                updated["target_epic_key"] = override_epic
                overrides_applied.append(issue_key or updated.get("operation_id", ""))

        filtered.append(updated)

    approved_for_execution = mode == "execute" and approved_flag
    result_value = approved_for_execution and bool(filtered)
    if mode != "execute":
        reason = "dry_run_mode"
    elif not approved_flag:
        reason = "approval_required"
    elif not filtered:
        reason = "no_operations_after_filters"
    else:
        reason = "approved"

    resolved_batch_size = _coerce_int(
        batch_size,
        default=_DEFAULT_BATCH_SIZE,
        minimum=1,
        maximum=_MAX_BATCH_SIZE,
    )

    return {
        "success": True,
        "execution_mode": mode,
        "approval_decision": {
            "approved": approved_for_execution,
            "reason": reason,
            "approved_by_user": approved_flag,
        },
        "approved_operations": filtered,
        "excluded_issue_keys_applied": sorted(set(excluded_applied)),
        "overrides_applied_for": sorted(set(item for item in overrides_applied if item)),
        "resolved_batch_size": resolved_batch_size,
        "result": result_value,
        "approval_checked_at_utc": _utc_now_iso(),
    }


def _result_is_success(response: Mapping[str, Any] | None) -> bool:
    if not isinstance(response, Mapping):
        return False
    if "success" in response:
        return bool(response.get("success"))
    if response.get("error"):
        return False
    return True


def _extract_error_text(response: Mapping[str, Any] | None) -> str:
    if not isinstance(response, Mapping):
        return "unknown_error"
    pieces: list[str] = []
    for key in ("error", "message", "error_code"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            pieces.append(value.strip())
    error_details = response.get("error_details")
    if isinstance(error_details, Mapping):
        for key in ("message", "status", "status_code", "reason"):
            value = error_details.get(key)
            if value is None:
                continue
            pieces.append(str(value))
    return " | ".join(pieces) if pieces else "unknown_error"


def _is_transient_response_error(response: Mapping[str, Any] | None) -> bool:
    error_text = _extract_error_text(response).lower()
    return any(marker in error_text for marker in _TRANSIENT_ERROR_MARKERS)


def execute_jira_hygiene_batches(
    *,
    update_issue: Callable[..., Mapping[str, Any]],
    add_comment: Callable[..., Mapping[str, Any]],
    approved_operations: Sequence[Mapping[str, Any]] | None,
    execution_mode: str | None,
    approved: Any,
    batch_size: int | None = None,
    max_retries: int | None = None,
    retry_backoff_seconds: float | int | None = None,
) -> dict[str, Any]:
    """Execute approved Jira hygiene operations with bounded retry/backoff."""
    operations = [
        dict(item) for item in (approved_operations or []) if isinstance(item, Mapping)
    ]
    mode = str(execution_mode or "dry_run").strip().lower()
    mode = "execute" if mode == "execute" else "dry_run"
    approved_flag = _coerce_bool(approved, default=False)

    resolved_batch_size = _coerce_int(
        batch_size,
        default=_DEFAULT_BATCH_SIZE,
        minimum=1,
        maximum=_MAX_BATCH_SIZE,
    )
    resolved_max_retries = _coerce_int(
        max_retries,
        default=_DEFAULT_MAX_RETRIES,
        minimum=0,
        maximum=_MAX_RETRIES,
    )
    try:
        resolved_backoff = float(
            retry_backoff_seconds
            if retry_backoff_seconds is not None
            else _DEFAULT_RETRY_BACKOFF_SECONDS
        )
    except Exception:
        resolved_backoff = _DEFAULT_RETRY_BACKOFF_SECONDS
    resolved_backoff = max(0.0, min(_MAX_RETRY_BACKOFF_SECONDS, resolved_backoff))

    if mode != "execute" or not approved_flag:
        return {
            "success": True,
            "execution_mode": mode,
            "execution_summary": {
                "executed": False,
                "total_operations": len(operations),
                "executed_operations_count": 0,
                "failed_operations_count": 0,
                "retry_attempts_total": 0,
                "batches_total": 0,
            },
            "execution_checkpoint": {
                "completed_operation_ids": [],
                "failed_operation_ids": [],
                "last_processed_index": -1,
                "updated_at_utc": _utc_now_iso(),
            },
            "executed_operations": [],
            "failed_operations": [],
            "result": False,
        }

    executed_operations: list[dict[str, Any]] = []
    failed_operations: list[dict[str, Any]] = []
    execution_events: list[dict[str, Any]] = []
    retry_attempts_total = 0
    batch_stats: dict[int, dict[str, int]] = {}

    for index, operation in enumerate(operations):
        issue_key = _normalise_issue_key(operation.get("issue_key")) or f"index-{index}"
        operation_id = str(operation.get("operation_id") or f"op-{index + 1}")
        op_kind = str(operation.get("operation") or "").strip().lower()
        batch_index = index // resolved_batch_size
        batch_bucket = batch_stats.setdefault(
            batch_index,
            {"operations": 0, "succeeded": 0, "failed": 0},
        )
        batch_bucket["operations"] += 1

        response_payload: Mapping[str, Any] | None = None
        operation_success = False
        final_error = "unknown_error"
        attempt = 0

        while attempt <= resolved_max_retries:
            attempt += 1
            if op_kind == "assign_epic":
                target_epic_key = _normalise_issue_key(operation.get("target_epic_key"))
                if not target_epic_key:
                    response_payload = {"success": False, "error": "target_epic_key_required"}
                else:
                    response_payload = update_issue(
                        issue_key=issue_key,
                        update_fields={"parent": {"key": target_epic_key}},
                        dry_run=False,
                        approved=True,
                    )
            elif op_kind == "add_comment":
                comment_text = operation.get("comment_text")
                if not isinstance(comment_text, str) or not comment_text.strip():
                    response_payload = {"success": False, "error": "comment_text_required"}
                else:
                    response_payload = add_comment(
                        issue_key=issue_key,
                        comment=comment_text.strip(),
                    )
            else:
                response_payload = {"success": False, "error": f"unsupported_operation:{op_kind}"}

            if _result_is_success(response_payload):
                operation_success = True
                break

            final_error = _extract_error_text(response_payload)
            is_transient = _is_transient_response_error(response_payload)
            if not is_transient or attempt > resolved_max_retries:
                break

            retry_attempts_total += 1
            sleep_seconds = resolved_backoff * attempt
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        event = {
            "operation_id": operation_id,
            "issue_key": issue_key,
            "operation": op_kind,
            "attempts": attempt,
            "success": operation_success,
            "error": None if operation_success else final_error,
            "batch_index": batch_index,
        }
        execution_events.append(event)

        if operation_success:
            batch_bucket["succeeded"] += 1
            executed_operations.append(
                {
                    "operation_id": operation_id,
                    "issue_key": issue_key,
                    "operation": op_kind,
                    "batch_index": batch_index,
                    "attempts": attempt,
                }
            )
        else:
            batch_bucket["failed"] += 1
            failed_operations.append(
                {
                    "operation_id": operation_id,
                    "issue_key": issue_key,
                    "operation": op_kind,
                    "batch_index": batch_index,
                    "attempts": attempt,
                    "error": final_error,
                }
            )

    batch_summaries = [
        {
            "batch_index": batch_index,
            "operations": values.get("operations", 0),
            "succeeded": values.get("succeeded", 0),
            "failed": values.get("failed", 0),
        }
        for batch_index, values in sorted(batch_stats.items())
    ]

    return {
        "success": len(failed_operations) == 0,
        "execution_mode": mode,
        "executed_operations": executed_operations,
        "failed_operations": failed_operations,
        "execution_events": execution_events,
        "execution_checkpoint": {
            "completed_operation_ids": [row["operation_id"] for row in executed_operations],
            "failed_operation_ids": [row["operation_id"] for row in failed_operations],
            "last_processed_index": len(operations) - 1,
            "updated_at_utc": _utc_now_iso(),
        },
        "execution_summary": {
            "executed": True,
            "total_operations": len(operations),
            "executed_operations_count": len(executed_operations),
            "failed_operations_count": len(failed_operations),
            "retry_attempts_total": retry_attempts_total,
            "batches_total": len(batch_summaries),
            "batch_summaries": batch_summaries,
        },
        "result": len(failed_operations) == 0,
    }


def emit_jira_hygiene_audit(
    *,
    add_comment: Callable[..., Mapping[str, Any]] | None,
    project_key: str | None,
    execution_mode: str | None,
    proposal_summary: Mapping[str, Any] | None,
    execution_summary: Mapping[str, Any] | None,
    approved_operations: Sequence[Mapping[str, Any]] | None = None,
    needs_decision: Sequence[Mapping[str, Any]] | None = None,
    emit_epic_comments: Any = False,
) -> dict[str, Any]:
    """Build final structured audit output, with optional epic comments."""
    mode = str(execution_mode or "dry_run").strip().lower()
    mode = "execute" if mode == "execute" else "dry_run"
    project_key_norm = str(project_key or _DEFAULT_PROJECT_KEY).strip().upper()
    proposal = dict(proposal_summary or {}) if isinstance(proposal_summary, Mapping) else {}
    execution = dict(execution_summary or {}) if isinstance(execution_summary, Mapping) else {}
    operations = [
        dict(item) for item in (approved_operations or []) if isinstance(item, Mapping)
    ]
    ambiguous = [dict(item) for item in (needs_decision or []) if isinstance(item, Mapping)]

    audit_comments: list[dict[str, Any]] = []
    should_emit_comments = (
        add_comment is not None
        and mode == "execute"
        and _coerce_bool(emit_epic_comments, default=False)
    )

    if should_emit_comments:
        add_comment_fn = add_comment
        assert add_comment_fn is not None
        grouped_by_epic: dict[str, int] = {}
        for operation in operations:
            if str(operation.get("operation") or "").strip().lower() != "assign_epic":
                continue
            epic_key = _normalise_issue_key(operation.get("target_epic_key"))
            if not epic_key:
                continue
            grouped_by_epic[epic_key] = grouped_by_epic.get(epic_key, 0) + 1

        for epic_key, moved_count in sorted(grouped_by_epic.items()):
            comment = (
                "Jira hygiene audit (auto-generated): "
                f"{moved_count} issue(s) were assigned to this epic in project {project_key_norm}."
            )
            response = add_comment_fn(issue_key=epic_key, comment=comment)
            audit_comments.append(
                {
                    "epic_key": epic_key,
                    "comment_text": comment,
                    "success": _result_is_success(response),
                    "response": dict(response) if isinstance(response, Mapping) else response,
                }
            )

    result_payload = {
        "success": bool(execution.get("executed", False) or mode == "dry_run")
        and int(execution.get("failed_operations_count", 0) or 0) == 0,
        "project_key": project_key_norm,
        "execution_mode": mode,
        "proposal_summary": proposal,
        "execution_summary": execution,
        "ambiguous_issues_remaining": [
            _normalise_issue_key(item.get("issue_key"))
            for item in ambiguous
            if _normalise_issue_key(item.get("issue_key"))
        ],
        "audit_comments": audit_comments,
        "generated_at_utc": _utc_now_iso(),
    }

    return {
        "success": True,
        "jira_hygiene_result": result_payload,
        "result": bool(result_payload.get("success")),
    }
