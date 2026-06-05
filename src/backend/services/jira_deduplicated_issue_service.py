"""Shared bounded Jira upsert support for deduplicated remediation issues."""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

_gateway_lock = Lock()
_gateway_singleton: Any | None = None


def _normalise_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _extract_jira_account_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("account_id", "accountId"):
        value = _normalise_text(payload.get(key))
        if value:
            return value
    user = payload.get("user")
    if isinstance(user, Mapping):
        return _normalise_text(user.get("accountId"))
    return None


def _extract_issue_key(payload: Mapping[str, Any]) -> str | None:
    for key in ("issue_key", "key"):
        value = _normalise_text(payload.get(key))
        if value:
            return value
    result = payload.get("result")
    if isinstance(result, Mapping):
        return _extract_issue_key(result)
    return None


def _extract_search_issue_keys(payload: Mapping[str, Any]) -> list[str]:
    issues = payload.get("issues")
    if not isinstance(issues, list):
        result = payload.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("issues"), list):
            issues = result.get("issues")
    if not isinstance(issues, list):
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        issue_key = _extract_issue_key(issue)
        if not issue_key:
            continue
        lowered = issue_key.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        keys.append(issue_key)
    return keys


def _normalise_labels(
    labels: Sequence[str] | None,
    *,
    search_label: str,
    fingerprint: str,
    fingerprint_label_prefix: str = "fp",
) -> list[str]:
    final_labels: list[str] = []
    seen: set[str] = set()
    candidate_values = [search_label]
    if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes, bytearray)):
        candidate_values.extend(labels)
    fingerprint_label = f"{fingerprint_label_prefix}-{fingerprint}"
    candidate_values.append(fingerprint_label[:255])

    for item in candidate_values:
        text = _normalise_text(item)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        final_labels.append(text[:255])
    return final_labels


def _get_gateway():
    global _gateway_singleton
    if _gateway_singleton is not None:
        return _gateway_singleton
    with _gateway_lock:
        if _gateway_singleton is not None:
            return _gateway_singleton
        from ..integrations.internal_mcp.catalogue import build_default_catalogue
        from ..integrations.internal_mcp.gateway import InternalMCPGateway
        from ..integrations.internal_mcp.transport import InternalMCPTransport

        _gateway_singleton = InternalMCPGateway(
            catalogue=build_default_catalogue(),
            transport=InternalMCPTransport(),
            enabled=True,
        )
        return _gateway_singleton


def _invoke_mcp_tool(tool_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        gateway = _get_gateway()
        result = gateway.invoke(tool_name, dict(payload))
        response_payload = result.payload if isinstance(result.payload, Mapping) else {}
        if isinstance(response_payload, Mapping):
            return dict(response_payload)
        return {"success": True, "result": result.payload}
    except Exception as exc:
        logger.warning(
            "[jira_deduplicated_issue_service] %s failed: %s", tool_name, exc
        )
        return {
            "success": False,
            "error_code": "mcp_invoke_failed",
            "error": f"{tool_name}:{exc}",
            "tool": tool_name,
        }


def upsert_deduplicated_jira_issue(
    *,
    project_key: str,
    issue_type: str,
    summary: str,
    description: str,
    fingerprint: str,
    search_label: str,
    labels: Sequence[str] | None = None,
    request_id: str | None = None,
    existing_comment: str | None = None,
    assignee_account_id: str | None = None,
    link_issue_key: str | None = None,
    link_type: str = "Relates",
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update a deduplicated Jira issue keyed by a stable fingerprint."""

    project = _normalise_text(project_key)
    issue_type_name = _normalise_text(issue_type)
    summary_text = _normalise_text(summary)
    description_text = _normalise_text(description)
    fingerprint_text = _normalise_text(fingerprint)
    search_label_text = _normalise_text(search_label)

    if (
        not project
        or not issue_type_name
        or not summary_text
        or not description_text
        or not fingerprint_text
        or not search_label_text
    ):
        return {
            "success": False,
            "error": "jira_remediation_payload_invalid",
            "error_code": "jira_remediation_payload_invalid",
        }

    search_jql = (
        f'project = "{project}" '
        f'AND labels = "{search_label_text}" '
        f'AND text ~ "\\"{fingerprint_text}\\"" '
        "ORDER BY updated DESC"
    )
    search_result = _invoke_mcp_tool(
        "jira_search",
        {
            "jql": search_jql,
            "max_results": 5,
            "fields": ["summary", "status", "assignee", "labels"],
        },
    )
    if not bool(search_result.get("success")):
        return {
            "success": False,
            "error": search_result.get("error") or "jira_search_failed",
            "error_code": search_result.get("error_code") or "jira_search_failed",
            "search_jql": search_jql,
        }

    existing_keys = _extract_search_issue_keys(search_result)
    if existing_keys:
        issue_key = existing_keys[0]
        link_result = _link_issue_if_requested(
            source_issue_key=issue_key,
            target_issue_key=link_issue_key,
            link_type=link_type,
            request_id=request_id,
            fingerprint=fingerprint_text,
        )
        if existing_comment:
            comment_result = _invoke_mcp_tool(
                "jira_add_comment",
                {"issue_key": issue_key, "comment": existing_comment},
            )
            if not bool(comment_result.get("success")):
                return {
                    "success": False,
                    "error": comment_result.get("error") or "jira_add_comment_failed",
                    "error_code": comment_result.get("error_code")
                    or "jira_add_comment_failed",
                    "issue_key": issue_key,
                    "search_jql": search_jql,
                }
        return {
            "success": True,
            "mode": "updated_existing",
            "issue_key": issue_key,
            "fingerprint": fingerprint_text,
            "search_jql": search_jql,
            "link_result": link_result,
        }

    resolved_assignee = _normalise_text(assignee_account_id)
    if not resolved_assignee:
        myself = _invoke_mcp_tool("jira_get_myself", {})
        resolved_assignee = _extract_jira_account_id(myself)
    if not resolved_assignee:
        return {
            "success": False,
            "error": "jira_assignee_resolution_failed",
            "error_code": "jira_assignee_resolution_failed",
            "search_jql": search_jql,
        }

    create_payload: dict[str, Any] = {
        "project_key": project,
        "issue_type": issue_type_name,
        "summary": summary_text,
        "description": description_text,
        "labels": _normalise_labels(
            labels,
            search_label=search_label_text,
            fingerprint=fingerprint_text,
        ),
        "assignee_account_id": resolved_assignee,
        "dry_run": False,
        "approved": True,
        "request_id": request_id or f"{search_label_text}:{fingerprint_text}",
    }
    if isinstance(fields, Mapping):
        create_payload.update(
            {
                str(key): value
                for key, value in fields.items()
                if isinstance(key, str) and str(key).strip()
            }
        )

    create_result = _invoke_mcp_tool("jira_create_issue", create_payload)
    if not bool(create_result.get("success")):
        return {
            "success": False,
            "error": create_result.get("error") or "jira_create_issue_failed",
            "error_code": create_result.get("error_code") or "jira_create_issue_failed",
            "search_jql": search_jql,
        }

    issue_key = _extract_issue_key(_coerce_mapping(create_result))
    link_result = _link_issue_if_requested(
        source_issue_key=issue_key,
        target_issue_key=link_issue_key,
        link_type=link_type,
        request_id=request_id,
        fingerprint=fingerprint_text,
    )
    return {
        "success": True,
        "mode": "created_new",
        "issue_key": issue_key,
        "fingerprint": fingerprint_text,
        "project_key": project,
        "search_jql": search_jql,
        "link_result": link_result,
    }


def _link_issue_if_requested(
    *,
    source_issue_key: str | None,
    target_issue_key: str | None,
    link_type: str,
    request_id: str | None,
    fingerprint: str,
) -> dict[str, Any] | None:
    source = _normalise_text(source_issue_key)
    target = _normalise_text(target_issue_key)
    link_name = _normalise_text(link_type) or "Relates"
    if not source or not target:
        return None
    return _invoke_mcp_tool(
        "jira_link_issue",
        {
            "source_issue_key": source,
            "target_issue_key": target,
            "link_type": link_name,
            "request_id": request_id or f"jira-link:{fingerprint}:{target}",
            "confirm": True,
        },
    )


__all__ = ["upsert_deduplicated_jira_issue"]
