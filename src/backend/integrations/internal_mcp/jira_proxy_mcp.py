"""Jira MCP proxy using the shared stdio client helper.

Bridges the Jira MCP server defined in src/backend/mcp_server/mcp_server.py so
Von's internal MCP gateway can call Jira tools without duplicating connection
logic.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, cast

from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError
from ...utils.runtime_env import apply_repo_dotenv_overrides, clean_env_value

logger = logging.getLogger(__name__)
_LOG_TAG = "[jira_proxy]"
JIRA_PROXY_ENV_OVERRIDE_KEYS: tuple[str, ...] = (
    "ATLASSIAN_BASE_URL",
    "ATLASSIAN_SITE_BASE",
    "ATLASSIAN_EMAIL",
    "ATLASSIAN_API_EMAIL",
    "ATLASSIAN_API_TOKEN",
)


class JiraProxyError(Exception):
    """Raised when Jira proxy operations fail."""


def _resolve_jira_auth(
    env: Mapping[str, str],
) -> tuple[str | None, str | None, str | None, dict[str, str | None]]:
    base_url_key = (
        "ATLASSIAN_BASE_URL"
        if clean_env_value(env.get("ATLASSIAN_BASE_URL"))
        else (
            "ATLASSIAN_SITE_BASE"
            if clean_env_value(env.get("ATLASSIAN_SITE_BASE"))
            else None
        )
    )
    email_key = (
        "ATLASSIAN_EMAIL"
        if clean_env_value(env.get("ATLASSIAN_EMAIL"))
        else (
            "ATLASSIAN_API_EMAIL"
            if clean_env_value(env.get("ATLASSIAN_API_EMAIL"))
            else None
        )
    )
    token_key = (
        "ATLASSIAN_API_TOKEN"
        if clean_env_value(env.get("ATLASSIAN_API_TOKEN"))
        else None
    )

    base_url = clean_env_value(env.get(base_url_key)) if base_url_key else None
    email = clean_env_value(env.get(email_key)) if email_key else None
    token = clean_env_value(env.get(token_key)) if token_key else None

    if base_url:
        base_url = base_url.rstrip("/")

    return base_url, email, token, {
        "base_url": base_url_key,
        "email": email_key,
        "token": token_key,
    }


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
        self,
        *,
        issue_key: str,
        fields: Optional[list[str]] = None,
        expand: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"issue_key": issue_key}
        if fields:
            arguments["fields"] = fields
        if expand:
            arguments["expand"] = expand
        return await self._call("jira_get_issue", arguments)

    async def get_comments(
        self,
        *,
        issue_key: str,
        start_at: Optional[int] = None,
        max_results: Optional[int] = None,
        order_by: Optional[str] = None,
        body_format: Optional[str] = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"issue_key": issue_key}
        if isinstance(start_at, int):
            arguments["start_at"] = start_at
        if isinstance(max_results, int):
            arguments["max_results"] = max_results
        if isinstance(order_by, str) and order_by.strip():
            arguments["order_by"] = order_by.strip()
        if isinstance(body_format, str) and body_format.strip():
            arguments["body_format"] = body_format.strip()
        return await self._call("jira_get_comments", arguments)

    async def get_watchers(self, *, issue_key: str) -> Dict[str, Any]:
        return await self._call("jira_get_watchers", {"issue_key": issue_key})

    async def get_attachment_content(
        self,
        *,
        attachment_id: str,
        max_size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"attachment_id": attachment_id}
        if isinstance(max_size_bytes, int):
            arguments["max_size_bytes"] = max_size_bytes
        return await self._call("jira_get_attachment_content", arguments)

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

    async def move_issue(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._call("jira_move_issue", {"payload": payload})

    async def get_bulk_operation_progress(self, *, task_id: str) -> Dict[str, Any]:
        return await self._call(
            "jira_get_bulk_operation_progress", {"task_id": task_id}
        )

    async def link_issue(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._call("jira_link_issue", {"payload": payload})

    async def delete_issue_link(self, *, issue_link_id: str) -> Dict[str, Any]:
        return await self._call(
            "jira_delete_issue_link", {"issue_link_id": issue_link_id}
        )

    async def get_myself(self) -> Dict[str, Any]:
        return await self._call("jira_get_myself", {})

    async def get_project_issue_types(self, *, project_key: str) -> Dict[str, Any]:
        return await self._call(
            "jira_get_project_issue_types", {"project_key": project_key}
        )

    def get_stats(self) -> Dict[str, int]:
        return {
            "call_count": self._client.call_count,
            "error_count": self._client.error_count,
        }


def _build_jira_env() -> Dict[str, str]:
    env = os.environ.copy()
    applied_overrides = apply_repo_dotenv_overrides(
        JIRA_PROXY_ENV_OVERRIDE_KEYS,
        environ=env,
    )
    if applied_overrides:
        logger.info(
            "%s Applied repo-root .env overrides for %s Jira key(s).",
            _LOG_TAG,
            len(applied_overrides),
        )

    base_url, email, token, env_keys_used = _resolve_jira_auth(env)

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

    logger.info(
        "%s Jira auth resolved from keys base_url=%s email=%s token_present=%s token_length=%d",
        _LOG_TAG,
        env_keys_used.get("base_url"),
        env_keys_used.get("email"),
        bool(token),
        len(token or ""),
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
    apply_repo_dotenv_overrides(
        JIRA_PROXY_ENV_OVERRIDE_KEYS,
        environ=env,
    )
    base_url, email, token, env_keys_used = _resolve_jira_auth(env)

    return {
        "success": True,
        "base_url": base_url,
        "email": email,
        "token_present": bool(token),
        "token_length": len(token) if token else 0,
        "env_keys_used": env_keys_used,
        "notes": (
            "This output reflects the effective Jira environment seen by the "
            "Von process after repo-root .env overrides are applied. "
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
# Catalogue calls can run on fresh event loops, so construction is loop-neutral.
_proxy_lock = threading.Lock()


async def get_jira_proxy() -> JiraMCPProxy:
    """Get or create the global Jira MCP proxy instance."""
    global _proxy_instance

    with _proxy_lock:
        if _proxy_instance is None:
            config = _build_jira_config()
            _proxy_instance = JiraMCPProxy(config)
            logger.info("%s Initialised Jira MCP proxy via %s", _LOG_TAG, config.args)
        return _proxy_instance
