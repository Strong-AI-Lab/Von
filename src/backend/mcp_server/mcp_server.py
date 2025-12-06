import os
import base64
import json
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

JIRA_BASE_URL = os.getenv("ATLASSIAN_BASE_URL", "https://naoinstitute.atlassian.net")
JIRA_EMAIL = os.getenv("ATLASSIAN_EMAIL")
JIRA_API_TOKEN = os.getenv("ATLASSIAN_API_TOKEN")

if not (JIRA_EMAIL and JIRA_API_TOKEN):
    raise RuntimeError("Missing ATLASSIAN_EMAIL or ATLASSIAN_API_TOKEN in environment")

auth_header = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()

HEADERS = {
    "Authorization": f"Basic {auth_header}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------
# Helper functions
# ---------------------------------------------------------


def jira_get(endpoint: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    return resp.json()


def jira_post(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    resp = requests.post(url, headers=HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()


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
                    "jql": {
                        "type": "string",
                        "description": "JQL query string"
                    }
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
                        "description": "Issue key, e.g. JVNAUTOSCI-371"
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
                        "description": "Transition ID from Jira"
                    },
                },
                "required": ["issue_key", "transition_id"],
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
        result = jira_get("search", params={"jql": jql})
        text = json.dumps(result, indent=2)
        return [types.TextContent(type="text", text=text)]

    elif name == "jira_get_issue":
        issue_key = arguments["issue_key"]
        result = jira_get(f"issue/{issue_key}")
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
