import os
import base64
import json
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


def jira_get(endpoint: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    return _request_json("GET", url, params=params)


def jira_post(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    return _request_json("POST", url, payload=payload)


def jira_put(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    return _request_json("PUT", url, payload=payload)


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
        result = jira_get(f"issue/{issue_key}", params=issue_params)
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_add_comment":
        issue_key = arguments["issue_key"]
        comment = arguments["comment"]
        payload = {"body": comment}
        result = jira_post(f"issue/{issue_key}/comment", payload)
        text = json.dumps(result, indent=2)
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

    elif name == "jira_create_issue":
        payload = arguments["payload"]
        if not isinstance(payload, dict):
            error = {
                "success": False,
                "error": "payload must be an object",
            }
            return [types.TextContent(type="text", text=json.dumps(error, indent=2))]
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
