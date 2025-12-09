"""Jira MCP proxy using the shared stdio client helper.

Bridges the Jira MCP server defined in src/backend/mcp_server/mcp_server.py so
Von's internal MCP gateway can call Jira tools without duplicating connection
logic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, cast

from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError

logger = logging.getLogger(__name__)
_LOG_TAG = "[jira_proxy]"


class JiraProxyError(Exception):
    """Raised when Jira proxy operations fail."""


@dataclass
class JiraProxyConfig:
    """Configuration for Jira MCP subprocess."""

    command: str
    args: list[str]
    env: Dict[str, str]
    timeout_sec: float = 30.0


class JiraMCPProxy:
    """Manages external Jira MCP server subprocess using MCP SDK client."""

    def __init__(self, config: JiraProxyConfig):
        client_config = MCPServerConfig(
            command=config.command,
            args=config.args,
            env=config.env,
            timeout_sec=config.timeout_sec,
            log_tag=_LOG_TAG,
        )
        self._client = MCPStdIOClient(client_config)

    async def _call(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        try:
            return await self._client.call_tool(tool_name, arguments)
        except MCPToolClientError as exc:
            raise JiraProxyError(str(exc)) from exc

    async def search(
        self,
        *,
        jql: str,
        max_results: Optional[int] = None,
        start_at: Optional[int] = None,
        fields: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"jql": jql}
        if isinstance(max_results, int):
            arguments["max_results"] = max_results
        if isinstance(start_at, int):
            arguments["start_at"] = start_at
        if fields:
            arguments["fields"] = fields
        return await self._call("jira_search", arguments)

    async def get_issue(self, *, issue_key: str, fields: Optional[list[str]] = None) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"issue_key": issue_key}
        if fields:
            arguments["fields"] = fields
        return await self._call("jira_get_issue", arguments)

    async def add_comment(self, *, issue_key: str, comment: str) -> Dict[str, Any]:
        return await self._call("jira_add_comment", {"issue_key": issue_key, "comment": comment})

    async def transition_issue(self, *, issue_key: str, transition_id: str) -> Dict[str, Any]:
        return await self._call("jira_transition", {"issue_key": issue_key, "transition_id": transition_id})

    def get_stats(self) -> Dict[str, int]:
        return {
            "call_count": self._client.call_count,
            "error_count": self._client.error_count,
        }


def _build_jira_env() -> Dict[str, str]:
    env = os.environ.copy()
    base_url = env.get("ATLASSIAN_BASE_URL") or env.get("ATLASSIAN_SITE_BASE")
    email = env.get("ATLASSIAN_EMAIL") or env.get("ATLASSIAN_API_EMAIL")
    token = env.get("ATLASSIAN_API_TOKEN")

    missing = [name for name, value in {
        "ATLASSIAN_BASE_URL": base_url,
        "ATLASSIAN_EMAIL": email,
        "ATLASSIAN_API_TOKEN": token,
    }.items() if not value]

    if missing:
        raise JiraProxyError(
            "Missing Atlassian settings: " + ", ".join(missing) + ". "
            "Set them in the environment, for example: "
            "$env:ATLASSIAN_BASE_URL='https://example.atlassian.net'; "
            "$env:ATLASSIAN_EMAIL='user@example.com'; "
            "$env:ATLASSIAN_API_TOKEN='token'"
        )

    env["ATLASSIAN_BASE_URL"] = cast(str, base_url)  # Normalise base key used by server
    env["ATLASSIAN_EMAIL"] = cast(str, email)
    env["ATLASSIAN_API_TOKEN"] = cast(str, token)
    return env


def _build_jira_config() -> JiraProxyConfig:
    env = _build_jira_env()
    script_path = Path(__file__).resolve().parents[2] / "mcp_server" / "mcp_server.py"
    if not script_path.exists():
        raise JiraProxyError(f"Jira MCP server script not found at {script_path}")
    return JiraProxyConfig(
        command=sys.executable,
        args=[str(script_path)],
        env=env,
    )


# Singleton instance
_proxy_instance: Optional[JiraMCPProxy] = None
_proxy_lock = asyncio.Lock()


async def get_jira_proxy() -> JiraMCPProxy:
    """Get or create the global Jira MCP proxy instance."""
    global _proxy_instance

    async with _proxy_lock:
        if _proxy_instance is None:
            config = _build_jira_config()
            _proxy_instance = JiraMCPProxy(config)
            logger.info("%s Initialised Jira MCP proxy via %s", _LOG_TAG, config.args)
        return _proxy_instance
