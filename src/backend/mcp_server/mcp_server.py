import os
import base64
import json
import re
import time
from typing import Any, Dict, List, Sequence

import anyio
import requests
from dotenv import load_dotenv

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# ---------------------------------------------------------
# Load env
# ---------------------------------------------------------

load_dotenv()


def _get_env(key: str, fallback: str | None = None) -> str | None:
    value = os.getenv(key)
    if value:
        return value
    return fallback


def _clean_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'"):
        cleaned = cleaned[1:-1].strip()
    return cleaned or None


# Prefer ATLASSIAN_* but fall back to legacy JIRA_MCP_* to reduce configuration errors
JIRA_BASE_URL = (
    _clean_env_value(_get_env("ATLASSIAN_BASE_URL", os.getenv("JIRA_MCP_BASE_URL")))
    or "https://naoinstitute.atlassian.net"
)
JIRA_EMAIL = _clean_env_value(_get_env("ATLASSIAN_EMAIL", os.getenv("JIRA_MCP_EMAIL")))
JIRA_API_TOKEN = _clean_env_value(
    _get_env("ATLASSIAN_API_TOKEN", os.getenv("JIRA_MCP_API_TOKEN"))
)

JIRA_BASE_URL = JIRA_BASE_URL.rstrip("/")

if not (JIRA_EMAIL and JIRA_API_TOKEN):
    raise RuntimeError(
        "Missing Atlassian credentials. Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN "
        "(or legacy JIRA_MCP_EMAIL/JIRA_MCP_API_TOKEN) in the environment."
    )

auth_header = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()

HEADERS = {
    "Authorization": f"Basic {auth_header}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------
# Helper functions
# ---------------------------------------------------------


def _jira_error_hint(status_code: int, *, url: str) -> str | None:
    if status_code == 401:
        return "Unauthorised: check Atlassian email/token validity."
    if status_code == 403:
        return "Forbidden: the Atlassian account may lack permission to view this issue/project."
    if status_code == 404 and "/rest/api/3/issue/" in url:
        return (
            "Not found: the issue key may be wrong, or Jira is hiding the issue due to permissions. "
            "Confirm the Atlassian account has Browse Projects permission and there is no issue security restriction."
        )
    return None


def _request_json(
    method: str,
    url: str,
    *,
    params: Dict[str, Any] | None = None,
    payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    max_attempts = 3
    delay_sec = 0.5

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(
                method.upper(),
                url,
                headers=HEADERS,
                params=params,
                json=payload,
                timeout=30,
            )

            if resp.ok:
                # Jira sometimes returns 204/empty bodies for updates/links.
                try:
                    body_text = resp.text
                except Exception:
                    body_text = ""
                if not body_text or not body_text.strip():
                    return {
                        "success": True,
                        "status_code": resp.status_code,
                        "url": url,
                    }
                try:
                    return resp.json()
                except Exception:
                    return {
                        "success": True,
                        "status_code": resp.status_code,
                        "url": url,
                        "response": body_text[:2000],
                    }

            # Rate limiting: bounded retry/backoff.
            if resp.status_code == 429 and attempt < max_attempts:
                retry_after_header = resp.headers.get("Retry-After")
                retry_after_sec: float | None = None
                if retry_after_header:
                    try:
                        retry_after_sec = float(retry_after_header)
                    except Exception:
                        retry_after_sec = None
                sleep_for = (
                    retry_after_sec if retry_after_sec is not None else delay_sec
                )
                time.sleep(max(0.1, min(sleep_for, 10.0)))
                delay_sec = min(delay_sec * 2.0, 8.0)
                continue

            hint = _jira_error_hint(resp.status_code, url=url)
            response_text = None
            try:
                response_text = resp.text
            except Exception:
                response_text = None

            error: Dict[str, Any] = {
                "success": False,
                "status_code": resp.status_code,
                "url": url,
                "error": f"Jira API HTTP {resp.status_code}",
            }
            if response_text:
                error["response"] = response_text[:2000]
            if hint:
                error["hint"] = hint
            return error
        except requests.exceptions.RequestException as exc:
            if attempt < max_attempts:
                time.sleep(max(0.1, min(delay_sec, 8.0)))
                delay_sec = min(delay_sec * 2.0, 8.0)
                continue
            return {"success": False, "error": f"Jira request failed: {exc}"}

    return {"success": False, "error": "Jira request failed after retries"}


def _request_bytes(url: str) -> Dict[str, Any]:
    max_attempts = 3
    delay_sec = 0.5

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(
                url,
                headers={
                    "Authorization": HEADERS["Authorization"],
                    "Accept": "*/*",
                },
                timeout=60,
            )

            if resp.ok:
                return {
                    "success": True,
                    "status_code": resp.status_code,
                    "url": url,
                    "content": resp.content,
                    "content_type": resp.headers.get("Content-Type"),
                    "content_length": resp.headers.get("Content-Length"),
                }

            if resp.status_code == 429 and attempt < max_attempts:
                retry_after_header = resp.headers.get("Retry-After")
                retry_after_sec: float | None = None
                if retry_after_header:
                    try:
                        retry_after_sec = float(retry_after_header)
                    except Exception:
                        retry_after_sec = None
                sleep_for = (
                    retry_after_sec if retry_after_sec is not None else delay_sec
                )
                time.sleep(max(0.1, min(sleep_for, 10.0)))
                delay_sec = min(delay_sec * 2.0, 8.0)
                continue

            response_text = None
            try:
                response_text = resp.text
            except Exception:
                response_text = None

            error: Dict[str, Any] = {
                "success": False,
                "status_code": resp.status_code,
                "url": url,
                "error": f"Jira API HTTP {resp.status_code}",
            }
            if response_text:
                error["response"] = response_text[:2000]
            return error
        except requests.exceptions.RequestException as exc:
            if attempt < max_attempts:
                time.sleep(max(0.1, min(delay_sec, 8.0)))
                delay_sec = min(delay_sec * 2.0, 8.0)
                continue
            return {"success": False, "error": f"Jira request failed: {exc}"}

    return {"success": False, "error": "Jira request failed after retries"}


def jira_get(endpoint: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    return _request_json("GET", url, params=params)


def jira_post(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    return _request_json("POST", url, payload=payload)


def jira_put(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    return _request_json("PUT", url, payload=payload)


def jira_delete(endpoint: str) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    return _request_json("DELETE", url)


def _simplify_jira_issue_type(value: Any) -> Dict[str, Any]:
    issue_type = value if isinstance(value, dict) else {}
    scope = issue_type.get("scope")
    project_scope = scope.get("project") if isinstance(scope, dict) else None
    simplified = {
        "id": issue_type.get("id"),
        "name": issue_type.get("name"),
        "description": issue_type.get("description"),
        "subtask": issue_type.get("subtask"),
    }
    hierarchy_level = issue_type.get("hierarchyLevel")
    if hierarchy_level is not None:
        simplified["hierarchyLevel"] = hierarchy_level
    if isinstance(scope, dict):
        simplified["scope"] = {
            "type": scope.get("type"),
            "project": (
                {
                    "id": project_scope.get("id"),
                    "key": project_scope.get("key"),
                    "name": project_scope.get("name"),
                }
                if isinstance(project_scope, dict)
                else None
            ),
        }
    return simplified


def jira_get_project_issue_types(project_key: str) -> Dict[str, Any]:
    project_key_norm = str(project_key or "").strip().upper()
    if not project_key_norm:
        return {"success": False, "error": "project_key is required"}

    project_result = jira_get(f"project/{project_key_norm}")
    if not isinstance(project_result, dict) or project_result.get("success") is False:
        error_payload = (
            dict(project_result)
            if isinstance(project_result, dict)
            else {"success": False, "error": "jira_project_lookup_failed"}
        )
        error_payload.setdefault("project_key", project_key_norm)
        return error_payload

    project_id = project_result.get("id")
    style = project_result.get("style")
    project_type_key = project_result.get("projectTypeKey")
    is_team_managed = style == "next-gen"

    project_issue_types_result = None
    if project_id is not None:
        project_issue_types_result = jira_get(
            "issuetype/project", params={"projectId": str(project_id)}
        )

    createmeta_result = None
    if project_id is not None:
        createmeta_result = jira_get(f"issue/createmeta/{project_id}/issuetypes")

    project_issue_types_raw = []
    if isinstance(project_issue_types_result, list):
        project_issue_types_raw = project_issue_types_result
    elif isinstance(project_issue_types_result, dict):
        values = project_issue_types_result.get("values")
        if isinstance(values, list):
            project_issue_types_raw = values

    creatable_issue_types_raw = []
    if isinstance(createmeta_result, dict):
        issue_types = createmeta_result.get("issueTypes")
        if isinstance(issue_types, list):
            creatable_issue_types_raw = issue_types

    project_issue_types = [
        _simplify_jira_issue_type(item) for item in project_issue_types_raw
    ]
    creatable_issue_types = [
        _simplify_jira_issue_type(item) for item in creatable_issue_types_raw
    ]

    available_names: list[str] = []
    for candidate in creatable_issue_types + project_issue_types:
        name = candidate.get("name")
        if isinstance(name, str) and name and name not in available_names:
            available_names.append(name)

    response: Dict[str, Any] = {
        "success": True,
        "project_key": project_key_norm,
        "project_id": str(project_id) if project_id is not None else None,
        "project_name": project_result.get("name"),
        "project_style": style,
        "project_type_key": project_type_key,
        "is_team_managed": is_team_managed,
        "issue_type_scheme_supported": not is_team_managed,
        "creatable_issue_types": creatable_issue_types,
        "project_issue_types": project_issue_types,
        "available_issue_type_names": available_names,
    }

    warnings: list[str] = []
    if isinstance(project_issue_types_result, dict) and project_issue_types_result.get(
        "success"
    ) is False:
        warnings.append("jira_project_issue_types_lookup_failed")
        response["project_issue_types_error"] = project_issue_types_result
    if isinstance(createmeta_result, dict) and createmeta_result.get("success") is False:
        warnings.append("jira_createmeta_issue_types_lookup_failed")
        response["createmeta_issue_types_error"] = createmeta_result
    if warnings:
        response["warnings"] = warnings

    return response


def jira_add_attachment(
    *,
    issue_key: str,
    filename: str,
    content_bytes: bytes,
    mime_type: str,
) -> Dict[str, Any] | List[Any]:
    """Upload an attachment to a Jira issue via multipart/form-data."""

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/attachments"
    headers = {
        "Authorization": HEADERS["Authorization"],
        "Accept": "application/json",
        "X-Atlassian-Token": "no-check",
    }

    max_attempts = 3
    delay_sec = 0.5

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                url,
                headers=headers,
                files={"file": (filename, content_bytes, mime_type)},
                timeout=30,
            )

            if resp.ok:
                try:
                    body_text = resp.text
                except Exception:
                    body_text = ""
                if not body_text or not body_text.strip():
                    return {"success": True, "status_code": resp.status_code, "url": url}
                try:
                    return resp.json()
                except Exception:
                    return {
                        "success": True,
                        "status_code": resp.status_code,
                        "url": url,
                        "response": body_text[:2000],
                    }

            if resp.status_code == 429 and attempt < max_attempts:
                retry_after_header = resp.headers.get("Retry-After")
                retry_after_sec: float | None = None
                if retry_after_header:
                    try:
                        retry_after_sec = float(retry_after_header)
                    except Exception:
                        retry_after_sec = None
                sleep_for = (
                    retry_after_sec if retry_after_sec is not None else delay_sec
                )
                time.sleep(max(0.1, min(sleep_for, 10.0)))
                delay_sec = min(delay_sec * 2.0, 8.0)
                continue

            hint = _jira_error_hint(resp.status_code, url=url)
            response_text = None
            try:
                response_text = resp.text
            except Exception:
                response_text = None

            error: Dict[str, Any] = {
                "success": False,
                "status_code": resp.status_code,
                "url": url,
                "error": f"Jira API HTTP {resp.status_code}",
            }
            if response_text:
                error["response"] = response_text[:2000]
            if hint:
                error["hint"] = hint
            return error
        except requests.exceptions.RequestException as exc:
            if attempt < max_attempts:
                time.sleep(max(0.1, min(delay_sec, 8.0)))
                delay_sec = min(delay_sec * 2.0, 8.0)
                continue
            return {"success": False, "error": f"Jira request failed: {exc}"}

    return {"success": False, "error": "Jira request failed after retries"}


def jira_get_attachment_content(
    *,
    attachment_id: str,
    max_size_bytes: int = 10 * 1024 * 1024,
) -> Dict[str, Any]:
    """Fetch Jira attachment metadata and base64-encoded bytes."""

    metadata = jira_get(f"attachment/{attachment_id}")
    if not isinstance(metadata, dict) or metadata.get("success") is False:
        return {
            "success": False,
            "attachment_id": attachment_id,
            "error": (
                metadata.get("error")
                if isinstance(metadata, dict)
                else "attachment_metadata_fetch_failed"
            ),
            "metadata": metadata if isinstance(metadata, dict) else None,
        }

    size_raw = metadata.get("size")
    size_bytes: int | None = None
    if isinstance(size_raw, int):
        size_bytes = size_raw
    elif isinstance(size_raw, str) and size_raw.strip().isdigit():
        size_bytes = int(size_raw.strip())
    if isinstance(size_bytes, int) and size_bytes > max_size_bytes:
        return {
            "success": False,
            "attachment_id": attachment_id,
            "error": "attachment_too_large",
            "size_bytes": size_bytes,
            "max_size_bytes": max_size_bytes,
            "metadata": metadata,
        }

    content_result = _request_bytes(
        f"{JIRA_BASE_URL}/rest/api/3/attachment/content/{attachment_id}"
    )
    if content_result.get("success") is not True:
        return {
            "success": False,
            "attachment_id": attachment_id,
            "error": content_result.get("error") or "attachment_content_fetch_failed",
            "metadata": metadata,
        }

    raw_content = content_result.get("content")
    if not isinstance(raw_content, (bytes, bytearray)):
        return {
            "success": False,
            "attachment_id": attachment_id,
            "error": "attachment_content_missing",
            "metadata": metadata,
        }

    content_bytes = bytes(raw_content)
    if len(content_bytes) > max_size_bytes:
        return {
            "success": False,
            "attachment_id": attachment_id,
            "error": "attachment_too_large",
            "size_bytes": len(content_bytes),
            "max_size_bytes": max_size_bytes,
            "metadata": metadata,
        }

    return {
        "success": True,
        "attachment_id": attachment_id,
        "filename": metadata.get("filename"),
        "size_bytes": size_bytes if isinstance(size_bytes, int) else len(content_bytes),
        "content_type": metadata.get("mimeType")
        or content_result.get("content_type"),
        "content_base64": base64.b64encode(content_bytes).decode("ascii"),
        "metadata": metadata,
    }


# ---------------------------------------------------------
# Markdown to Atlassian Document Format (ADF) converter
# ---------------------------------------------------------

def _markdown_to_adf(text: str) -> Dict[str, Any]:
    """Convert Markdown/plain text to Atlassian Document Format (ADF).

    Supports:
    - Paragraphs (blank line separated)
    - Headings (# to ######)
    - Bullet lists (- or *)
    - Numbered lists (1. 2. etc.)
    - Code blocks (```)
    - Inline formatting: **bold**, *italic*, `code`, [links](url)
    """
    if not text or not text.strip():
        return {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": []}],
        }

    lines = text.split("\n")
    content: List[Dict[str, Any]] = []
    i = 0

    def parse_inline(line: str) -> List[Dict[str, Any]]:
        """Parse inline formatting: bold, italic, code, links."""
        result: List[Dict[str, Any]] = []
        # Pattern to match inline elements
        # Order matters: links first, then code, then bold, then italic
        pattern = re.compile(
            r"(\[([^\]]+)\]\(([^)]+)\))"  # [text](url)
            r"|(`[^`]+`)"  # `code`
            r"|(\*\*[^*]+\*\*)"  # **bold**
            r"|(__[^_]+__)"  # __bold__
            r"|(\*[^*]+\*)"  # *italic*
            r"|(_[^_]+_)"  # _italic_
        )
        last_end = 0
        for match in pattern.finditer(line):
            # Add text before match
            if match.start() > last_end:
                before = line[last_end : match.start()]
                if before:
                    result.append({"type": "text", "text": before})

            full = match.group(0)
            if full.startswith("[") and "](" in full:
                # Link
                link_match = re.match(r"\[([^\]]+)\]\(([^)]+)\)", full)
                if link_match:
                    result.append(
                        {
                            "type": "text",
                            "text": link_match.group(1),
                            "marks": [
                                {"type": "link", "attrs": {"href": link_match.group(2)}}
                            ],
                        }
                    )
            elif full.startswith("`") and full.endswith("`"):
                # Inline code
                result.append(
                    {
                        "type": "text",
                        "text": full[1:-1],
                        "marks": [{"type": "code"}],
                    }
                )
            elif (full.startswith("**") and full.endswith("**")) or (
                full.startswith("__") and full.endswith("__")
            ):
                # Bold
                result.append(
                    {
                        "type": "text",
                        "text": full[2:-2],
                        "marks": [{"type": "strong"}],
                    }
                )
            elif (full.startswith("*") and full.endswith("*")) or (
                full.startswith("_") and full.endswith("_")
            ):
                # Italic
                result.append(
                    {
                        "type": "text",
                        "text": full[1:-1],
                        "marks": [{"type": "em"}],
                    }
                )
            last_end = match.end()

        # Add remaining text
        if last_end < len(line):
            remaining = line[last_end:]
            if remaining:
                result.append({"type": "text", "text": remaining})

        # If no matches, return entire line as text
        if not result and line:
            result.append({"type": "text", "text": line})

        return result

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Empty line - skip (paragraph breaks handled by grouping)
        if not stripped:
            i += 1
            continue

        # Code block
        if stripped.startswith("```"):
            lang = stripped[3:].strip() or None
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code_block: Dict[str, Any] = {
                "type": "codeBlock",
                "content": [{"type": "text", "text": "\n".join(code_lines)}],
            }
            if lang:
                code_block["attrs"] = {"language": lang}
            content.append(code_block)
            continue

        # Heading
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2)
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": level},
                    "content": parse_inline(heading_text),
                }
            )
            i += 1
            continue

        # Bullet list
        if re.match(r"^[-*]\s+", stripped):
            list_items: List[Dict[str, Any]] = []
            while i < len(lines):
                item_line = lines[i].strip()
                item_match = re.match(r"^[-*]\s+(.+)$", item_line)
                if item_match:
                    list_items.append(
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": parse_inline(item_match.group(1)),
                                }
                            ],
                        }
                    )
                    i += 1
                elif not item_line:
                    # Empty line might end the list or be between items
                    if i + 1 < len(lines) and re.match(
                        r"^[-*]\s+", lines[i + 1].strip()
                    ):
                        i += 1
                        continue
                    break
                else:
                    break
            if list_items:
                content.append({"type": "bulletList", "content": list_items})
            continue

        # Numbered list
        if re.match(r"^\d+\.\s+", stripped):
            list_items = []
            while i < len(lines):
                item_line = lines[i].strip()
                item_match = re.match(r"^\d+\.\s+(.+)$", item_line)
                if item_match:
                    list_items.append(
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": parse_inline(item_match.group(1)),
                                }
                            ],
                        }
                    )
                    i += 1
                elif not item_line:
                    if i + 1 < len(lines) and re.match(
                        r"^\d+\.\s+", lines[i + 1].strip()
                    ):
                        i += 1
                        continue
                    break
                else:
                    break
            if list_items:
                content.append({"type": "orderedList", "content": list_items})
            continue

        # Regular paragraph - collect consecutive non-empty lines
        para_lines = []
        while i < len(lines):
            current = lines[i].strip()
            if (
                not current
                or current.startswith("#")
                or current.startswith("```")
                or re.match(r"^[-*]\s+", current)
                or re.match(r"^\d+\.\s+", current)
            ):
                break
            para_lines.append(current)
            i += 1
        if para_lines:
            # Join lines with space and parse inline
            para_text = " ".join(para_lines)
            content.append(
                {
                    "type": "paragraph",
                    "content": parse_inline(para_text),
                }
            )

    # If no content was generated, add empty paragraph
    if not content:
        content.append({"type": "paragraph", "content": []})

    return {"type": "doc", "version": 1, "content": content}


def _ensure_adf(value: Any) -> Dict[str, Any] | Any:
    """Convert a string to ADF if needed. Pass through if already ADF or not a string."""
    if isinstance(value, str):
        return _markdown_to_adf(value)
    return value


# ---------------------------------------------------------
# MCP server
# ---------------------------------------------------------

server = Server("jira-mcp")


# 1) Tell the client which tools exist
@server.list_tools()
async def list_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name="jira_search",
            description="Run a JQL query in Jira and return matching issues.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jql": {"type": "string", "description": "JQL query string"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional Jira fields to include (e.g. ['summary','status']).",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of issues to return (Jira max is typically 100).",
                    },
                    "next_page_token": {
                        "type": "string",
                        "description": "Pagination token returned by Jira /search/jql (preferred over deprecated start_at).",
                    },
                    "start_at": {
                        "type": "integer",
                        "description": "Deprecated: Jira /search/jql uses next_page_token pagination (cursor-based).",
                    },
                },
                "required": ["jql"],
            },
        ),
        types.Tool(
            name="jira_get_issue",
            description="Get full details of a Jira issue by key.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key, e.g. JVNAUTOSCI-371",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional Jira fields to include (e.g. ['summary','status']).",
                    },
                    "expand": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional Jira expand tokens (e.g. ['changelog']).",
                    },
                },
                "required": ["issue_key"],
            },
        ),
        types.Tool(
            name="jira_get_watchers",
            description="List watcher identities for a Jira issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key, e.g. JVNAUTOSCI-371",
                    }
                },
                "required": ["issue_key"],
            },
        ),
        types.Tool(
            name="jira_get_attachment_content",
            description="Fetch Jira attachment bytes and return base64 content plus metadata.",
            inputSchema={
                "type": "object",
                "properties": {
                    "attachment_id": {
                        "type": "string",
                        "description": "Jira attachment ID.",
                    },
                    "max_size_bytes": {
                        "type": "integer",
                        "description": "Optional maximum attachment size to fetch.",
                    },
                },
                "required": ["attachment_id"],
            },
        ),
        types.Tool(
            name="jira_get_transitions",
            description="List available workflow transitions for a Jira issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key, e.g. JVNAUTOSCI-371",
                    }
                },
                "required": ["issue_key"],
            },
        ),
        types.Tool(
            name="jira_add_comment",
            description="Add a comment to a Jira issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["issue_key", "comment"],
            },
        ),
        types.Tool(
            name="jira_add_attachment",
            description=(
                "Add an attachment to a Jira issue. Requires base64 content, "
                "MIME type, and safe filename."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                    "filename": {"type": "string"},
                    "content_base64": {
                        "type": "string",
                        "description": "Base64-encoded file content",
                    },
                    "mime_type": {"type": "string"},
                    "comment": {
                        "type": "string",
                        "description": "Optional follow-up Jira comment text",
                    },
                },
                "required": ["issue_key", "filename", "content_base64", "mime_type"],
            },
        ),
        types.Tool(
            name="jira_transition",
            description="Transition a Jira issue using a transition ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                    "transition_id": {
                        "type": "string",
                        "description": "Transition ID from Jira",
                    },
                },
                "required": ["issue_key", "transition_id"],
            },
        ),
        types.Tool(
            name="jira_get_myself",
            description=(
                "Return the Jira user profile for the currently authenticated Atlassian credentials. "
                "Useful for debugging permission-related 404s."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="jira_get_project_issue_types",
            description=(
                "Return Jira project style and the issue types currently configured/creatable "
                "for a project key."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "Project key, e.g. JVNAUTOSCI",
                    }
                },
                "required": ["project_key"],
            },
        ),
        types.Tool(
            name="jira_create_issue",
            description=(
                "Create a Jira issue via POST /rest/api/3/issue. "
                "The caller must provide the exact Jira payload dict (fields, etc.)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": "Jira create issue payload to send as JSON body.",
                    }
                },
                "required": ["payload"],
            },
        ),
        types.Tool(
            name="jira_update_issue",
            description=(
                "Update a Jira issue via PUT /rest/api/3/issue/{issue_key}. "
                "The caller must provide the exact Jira payload dict (fields, update, etc.)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key, e.g. JVNAUTOSCI-371",
                    },
                    "payload": {
                        "type": "object",
                        "description": "Jira update issue payload to send as JSON body.",
                    },
                },
                "required": ["issue_key", "payload"],
            },
        ),
        types.Tool(
            name="jira_link_issue",
            description=(
                "Create an issue link via POST /rest/api/3/issueLink. "
                "The caller must provide the exact Jira payload dict (type, inwardIssue, outwardIssue, etc.)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": "Jira issueLink payload to send as JSON body.",
                    }
                },
                "required": ["payload"],
            },
        ),
        types.Tool(
            name="jira_delete_issue_link",
            description=(
                "Delete an issue link via DELETE /rest/api/3/issueLink/{issue_link_id}. "
                "Requires a Jira issue link ID."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_link_id": {
                        "type": "string",
                        "description": "Jira issue link ID",
                    }
                },
                "required": ["issue_link_id"],
            },
        ),
    ]


# 2) Handle actual tool calls
@server.call_tool()
async def call_tool(
    name: str, arguments: Dict[str, Any]
) -> Sequence[types.TextContent]:
    if name == "jira_search":
        jql = arguments["jql"]
        payload: Dict[str, Any] = {
            "jql": jql,
            "maxResults": 50,
        }
        max_results = arguments.get("max_results")
        if isinstance(max_results, int):
            payload["maxResults"] = max_results

        # Jira Cloud /rest/api/3/search/jql uses cursor pagination via nextPageToken.
        # Avoid sending deprecated startAt, as some instances reject it with HTTP 400.
        start_at = arguments.get("start_at")
        if isinstance(start_at, int) and start_at not in (0, None):
            error = {
                "success": False,
                "error": "Deprecated pagination parameter: start_at. Use next_page_token instead.",
            }
            return [types.TextContent(type="text", text=json.dumps(error, indent=2))]

        next_page_token = arguments.get("next_page_token")
        if isinstance(next_page_token, str) and next_page_token.strip():
            payload["nextPageToken"] = next_page_token.strip()

        fields = arguments.get("fields")
        if isinstance(fields, list) and fields:
            payload["fields"] = [str(f) for f in fields]

        # Jira Cloud is deprecating GET /search?jql=... for some usage; prefer POST /search/jql.
        result = jira_post("search/jql", payload)
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_get_issue":
        issue_key = arguments["issue_key"]
        issue_params: Dict[str, Any] | None = None
        fields = arguments.get("fields")
        if isinstance(fields, list) and fields:
            issue_params = {"fields": ",".join(str(f) for f in fields)}
        expand = arguments.get("expand")
        if isinstance(expand, list) and expand:
            if issue_params is None:
                issue_params = {}
            issue_params["expand"] = ",".join(str(item) for item in expand)
        result = jira_get(f"issue/{issue_key}", params=issue_params)
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_get_watchers":
        issue_key = arguments["issue_key"]
        result = jira_get(f"issue/{issue_key}/watchers")
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_get_attachment_content":
        attachment_id = arguments["attachment_id"]
        raw_max_size = arguments.get("max_size_bytes")
        if isinstance(raw_max_size, (int, str)):
            try:
                max_size_bytes = int(raw_max_size)
            except ValueError:
                max_size_bytes = 10 * 1024 * 1024
        else:
            max_size_bytes = 10 * 1024 * 1024
        if max_size_bytes <= 0:
            max_size_bytes = 10 * 1024 * 1024
        result = jira_get_attachment_content(
            attachment_id=str(attachment_id),
            max_size_bytes=max_size_bytes,
        )
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_get_transitions":
        issue_key = arguments["issue_key"]
        result = jira_get(f"issue/{issue_key}/transitions")
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_add_comment":
        issue_key = arguments["issue_key"]
        comment = arguments["comment"]
        # Jira Cloud requires ADF for comment body
        payload = {"body": _ensure_adf(comment)}
        result = jira_post(f"issue/{issue_key}/comment", payload)
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_add_attachment":
        issue_key = arguments.get("issue_key")
        filename = arguments.get("filename")
        content_base64 = arguments.get("content_base64")
        mime_type = arguments.get("mime_type")
        comment = arguments.get("comment")

        missing = []
        for key in ("issue_key", "filename", "content_base64", "mime_type"):
            value = arguments.get(key)
            if not isinstance(value, str) or not value.strip():
                missing.append(key)
        if missing:
            error = {
                "success": False,
                "error": f"Missing required parameters: {', '.join(missing)}",
            }
            return [types.TextContent(type="text", text=json.dumps(error, indent=2))]

        try:
            content_bytes = base64.b64decode(str(content_base64), validate=True)
        except Exception:
            error = {
                "success": False,
                "error": "Invalid content_base64 payload",
            }
            return [types.TextContent(type="text", text=json.dumps(error, indent=2))]

        upload_result = jira_add_attachment(
            issue_key=str(issue_key).strip(),
            filename=str(filename).strip(),
            content_bytes=content_bytes,
            mime_type=str(mime_type).strip(),
        )

        if isinstance(upload_result, dict) and upload_result.get("success") is False:
            text = json.dumps(upload_result, indent=2)
            return [types.TextContent(type="text", text=text)]

        attachments: List[Dict[str, Any]] = []
        if isinstance(upload_result, list):
            attachments = [
                item for item in upload_result if isinstance(item, dict)
            ]
        elif isinstance(upload_result, dict):
            attachments = [upload_result]

        response: Dict[str, Any] = {
            "success": True,
            "issue_key": str(issue_key).strip(),
            "attachments": attachments,
        }

        if isinstance(comment, str) and comment.strip():
            comment_payload = {"body": _ensure_adf(comment.strip())}
            comment_result = jira_post(
                f"issue/{str(issue_key).strip()}/comment", comment_payload
            )
            if isinstance(comment_result, dict) and comment_result.get("success") is False:
                response["comment_added"] = False
                response["comment_error"] = comment_result
            else:
                response["comment_added"] = True
                response["comment_result"] = comment_result

        text = json.dumps(response, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_transition":
        issue_key = arguments["issue_key"]
        transition_id = arguments["transition_id"]
        payload = {"transition": {"id": transition_id}}
        result = jira_post(f"issue/{issue_key}/transitions", payload)
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_get_myself":
        result = jira_get("myself")
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_get_project_issue_types":
        project_key = arguments["project_key"]
        result = jira_get_project_issue_types(str(project_key))
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_create_issue":
        payload = arguments["payload"]
        if not isinstance(payload, dict):
            error = {
                "success": False,
                "error": "payload must be an object",
            }
            return [types.TextContent(type="text", text=json.dumps(error, indent=2))]
        # Convert description to ADF if present and is a string
        if "fields" in payload and isinstance(payload["fields"], dict):
            if "description" in payload["fields"]:
                payload["fields"]["description"] = _ensure_adf(
                    payload["fields"]["description"]
                )
        result = jira_post("issue", payload)
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_update_issue":
        issue_key = arguments["issue_key"]
        payload = arguments["payload"]
        if not isinstance(payload, dict):
            error = {
                "success": False,
                "error": "payload must be an object",
            }
            return [types.TextContent(type="text", text=json.dumps(error, indent=2))]
        # Convert description to ADF if present and is a string
        if "fields" in payload and isinstance(payload["fields"], dict):
            if "description" in payload["fields"]:
                payload["fields"]["description"] = _ensure_adf(
                    payload["fields"]["description"]
                )
        result = jira_put(f"issue/{issue_key}", payload)
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_link_issue":
        payload = arguments["payload"]
        if not isinstance(payload, dict):
            error = {
                "success": False,
                "error": "payload must be an object",
            }
            return [types.TextContent(type="text", text=json.dumps(error, indent=2))]
        result = jira_post("issueLink", payload)
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_delete_issue_link":
        issue_link_id = str(arguments["issue_link_id"]).strip()
        if not issue_link_id:
            error = {
                "success": False,
                "error": "issue_link_id is required",
            }
            return [types.TextContent(type="text", text=json.dumps(error, indent=2))]
        result = jira_delete(f"issueLink/{issue_link_id}")
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    else:
        return [
            types.TextContent(
                type="text",
                text=f"Unknown tool: {name}",
            )
        ]


# 3) Run over stdio for Desktop App MCP
async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    anyio.run(main)
