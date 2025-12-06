#!/usr/bin/env python3
"""
MCP stdio server for Jira operations.
Provides tools for searching issues, getting details, adding comments, and transitioning issues.
"""

import os
import sys
import base64
import json
import asyncio
from typing import Any, Dict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("Error: MCP package not installed. Run: pdm add mcp", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------
# Load env
# ---------------------------------------------------------

# Find project root (where .env lives)
project_root = Path(__file__).resolve().parents[4]  # Go up from jira/ -> mcp_server/ -> backend/ -> src/ -> Von/
dotenv_path = project_root / ".env"
load_dotenv(dotenv_path=dotenv_path)

JIRA_BASE_URL = os.getenv("JIRA_MCP_BASE_URL", "https://naoinstitute.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_MCP_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_MCP_API_TOKEN")

if not (JIRA_EMAIL and JIRA_API_TOKEN):
    print(
        "Error: Missing JIRA_MCP_EMAIL or JIRA_MCP_API_TOKEN in environment",
        file=sys.stderr,
    )
    sys.exit(1)

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
    """Execute GET request to Jira API."""
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def jira_post(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute POST request to Jira API."""
    url = f"{JIRA_BASE_URL}/rest/api/3/{endpoint}"
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------
# MCP server
# ---------------------------------------------------------

server = Server("jira-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available Jira tools."""
    return [
        Tool(
            name="jira_search",
            description="Run a JQL query and return matching issues",
            inputSchema={
                "type": "object",
                "properties": {
                    "jql": {
                        "type": "string",
                        "description": "JQL query string (e.g., 'project = JVNAUTOSCI AND status = Open')",
                    }
                },
                "required": ["jql"],
            },
        ),
        Tool(
            name="jira_get_issue",
            description="Get full details about a Jira issue",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key (e.g., 'JVNAUTOSCI-123')",
                    }
                },
                "required": ["issue_key"],
            },
        ),
        Tool(
            name="jira_add_comment",
            description="Add a comment to a Jira issue",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key (e.g., 'JVNAUTOSCI-123')",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Comment text to add",
                    },
                },
                "required": ["issue_key", "comment"],
            },
        ),
        Tool(
            name="jira_transition",
            description="Transition a Jira issue to a new status",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key (e.g., 'JVNAUTOSCI-123')",
                    },
                    "transition_id": {
                        "type": "string",
                        "description": "Transition ID (get from Jira API or UI)",
                    },
                },
                "required": ["issue_key", "transition_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute a Jira tool with given arguments."""
    try:
        if name == "jira_search":
            jql = arguments.get("jql", "")
            result = jira_get("search", params={"jql": jql})
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2),
                )
            ]

        elif name == "jira_get_issue":
            issue_key = arguments.get("issue_key", "")
            result = jira_get(f"issue/{issue_key}")
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2),
                )
            ]

        elif name == "jira_add_comment":
            issue_key = arguments.get("issue_key", "")
            comment = arguments.get("comment", "")
            payload = {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": comment}],
                        }
                    ],
                }
            }
            result = jira_post(f"issue/{issue_key}/comment", payload)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2),
                )
            ]

        elif name == "jira_transition":
            issue_key = arguments.get("issue_key", "")
            transition_id = arguments.get("transition_id", "")
            payload = {"transition": {"id": transition_id}}
            result = jira_post(f"issue/{issue_key}/transitions", payload)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2),
                )
            ]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except requests.exceptions.RequestException as e:
        error_msg = f"Jira API error: {str(e)}"
        if e.response is not None and hasattr(e.response, "text"):
            error_msg += f"\nResponse: {e.response.text}"
        return [TextContent(type="text", text=error_msg)]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main() -> None:
    """Main entry point - runs the MCP server over stdio."""
    print(f"Starting Jira MCP server at {datetime.now()}", file=sys.stderr)
    print(f"Connected to: {JIRA_BASE_URL}", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
