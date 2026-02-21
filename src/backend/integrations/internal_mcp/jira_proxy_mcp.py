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
        next_page_token: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"jql": jql}
        if isinstance(max_results, int):
            arguments["max_results"] = max_results
        if isinstance(start_at, int):
            arguments["start_at"] = start_at
        if isinstance(next_page_token, str) and next_page_token.strip():
            arguments["next_page_token"] = next_page_token.strip()
        if fields:
            arguments["fields"] = fields
        return await self._call("jira_search", arguments)

    async def get_issue(
        self, *, issue_key: str, fields: Optional[list[str]] = None
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"issue_key": issue_key}
        if fields:
            arguments["fields"] = fields
        return await self._call("jira_get_issue", arguments)

    async def get_transitions(self, *, issue_key: str) -> Dict[str, Any]:
        return await self._call("jira_get_transitions", {"issue_key": issue_key})

    async def add_comment(self, *, issue_key: str, comment: str) -> Dict[str, Any]:
        return await self._call(
            "jira_add_comment", {"issue_key": issue_key, "comment": comment}
        )

    async def add_attachment(
        self,
        *,
        issue_key: str,
        filename: str,
        content_base64: str,
        mime_type: str,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {
            "issue_key": issue_key,
            "filename": filename,
            "content_base64": content_base64,
            "mime_type": mime_type,
        }
        if isinstance(comment, str) and comment.strip():
            arguments["comment"] = comment.strip()
        return await self._call("jira_add_attachment", arguments)

    async def transition_issue(
        self, *, issue_key: str, transition_id: str
    ) -> Dict[str, Any]:
        return await self._call(
            "jira_transition", {"issue_key": issue_key, "transition_id": transition_id}
        )

    async def create_issue(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._call("jira_create_issue", {"payload": payload})

    async def update_issue(
        self, *, issue_key: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._call(
            "jira_update_issue", {"issue_key": issue_key, "payload": payload}
        )

    async def link_issue(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._call("jira_link_issue", {"payload": payload})

    async def delete_issue_link(self, *, issue_link_id: str) -> Dict[str, Any]:
        return await self._call(
            "jira_delete_issue_link", {"issue_link_id": issue_link_id}
        )

    async def get_myself(self) -> Dict[str, Any]:
        return await self._call("jira_get_myself", {})

    def get_stats(self) -> Dict[str, int]:
        return {
            "call_count": self._client.call_count,
            "error_count": self._client.error_count,
        }


def _build_jira_env() -> Dict[str, str]:
    env = os.environ.copy()

    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'"):
            cleaned = cleaned[1:-1].strip()
        return cleaned or None

    base_url = _clean(env.get("ATLASSIAN_BASE_URL") or env.get("ATLASSIAN_SITE_BASE"))
    email = _clean(env.get("ATLASSIAN_EMAIL") or env.get("ATLASSIAN_API_EMAIL"))
    token = _clean(env.get("ATLASSIAN_API_TOKEN"))

    if base_url:
        base_url = base_url.rstrip("/")

    missing = [
        name
        for name, value in {
            "ATLASSIAN_BASE_URL": base_url,
            "ATLASSIAN_EMAIL": email,
            "ATLASSIAN_API_TOKEN": token,
        }.items()
        if not value
    ]

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


def inspect_jira_auth_config() -> Dict[str, Any]:
    """Return the Jira auth configuration currently visible to the process.

    This is a diagnostics helper for debugging authentication issues. It does
    not contact Jira and it never returns the API token.
    """

    env = os.environ.copy()

    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'"):
            cleaned = cleaned[1:-1].strip()
        return cleaned or None

    base_url_key = (
        "ATLASSIAN_BASE_URL"
        if env.get("ATLASSIAN_BASE_URL")
        else ("ATLASSIAN_SITE_BASE" if env.get("ATLASSIAN_SITE_BASE") else None)
    )
    email_key = (
        "ATLASSIAN_EMAIL"
        if env.get("ATLASSIAN_EMAIL")
        else ("ATLASSIAN_API_EMAIL" if env.get("ATLASSIAN_API_EMAIL") else None)
    )
    token_key = "ATLASSIAN_API_TOKEN" if env.get("ATLASSIAN_API_TOKEN") else None

    base_url = _clean(env.get(base_url_key)) if base_url_key else None
    email = _clean(env.get(email_key)) if email_key else None
    token_raw = env.get(token_key) if token_key else None
    token = _clean(token_raw)

    if base_url:
        base_url = base_url.rstrip("/")

    return {
        "success": True,
        "base_url": base_url,
        "email": email,
        "token_present": bool(token),
        "token_length": len(token) if token else 0,
        "env_keys_used": {
            "base_url": base_url_key,
            "email": email_key,
            "token": token_key,
        },
        "notes": (
            "This output reflects environment variables read by the Von process. "
            "It does not prove Jira authentication is valid."
        ),
    }


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
