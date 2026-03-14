"""GitHub MCP proxy using the shared stdio client helper.

This bridges an external GitHub MCP server so Von's internal MCP gateway can
invoke GitHub tools through a single managed stdio client.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError

logger = logging.getLogger(__name__)
_LOG_TAG = "[github_proxy]"


class GitHubProxyError(Exception):
    """Raised when GitHub proxy operations fail."""


@dataclass
class GitHubProxyConfig:
    """Configuration for GitHub MCP subprocess."""

    command: str
    args: list[str]
    env: Dict[str, str]
    timeout_sec: float = 30.0


class GitHubMCPProxy:
    """Manages external GitHub MCP server subprocess using MCP SDK client."""

    def __init__(self, config: GitHubProxyConfig):
        client_config = MCPServerConfig(
            command=config.command,
            args=config.args,
            env=config.env,
            timeout_sec=config.timeout_sec,
            log_tag=_LOG_TAG,
        )
        self._client = MCPStdIOClient(client_config)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        try:
            return await self._client.call_tool(tool_name, arguments)
        except MCPToolClientError as exc:
            raise GitHubProxyError(str(exc)) from exc

    async def list_tools(self) -> list[Dict[str, Any]]:
        try:
            return await self._client.list_tools()
        except MCPToolClientError as exc:
            raise GitHubProxyError(str(exc)) from exc

    def get_stats(self) -> Dict[str, int]:
        return {
            "call_count": self._client.call_count,
            "error_count": self._client.error_count,
        }


def _build_github_env() -> Dict[str, str]:
    env = os.environ.copy()

    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'"):
            cleaned = cleaned[1:-1].strip()
        return cleaned or None

    token = _clean(
        env.get("GITHUB_PERSONAL_ACCESS_TOKEN")
        or env.get("GITHUB_VON_TOKEN")
        or env.get("GITHUB_TOKEN")
        or env.get("GH_TOKEN")
    )
    if not token:
        raise GitHubProxyError(
            "Missing GitHub token. Set one of: GITHUB_PERSONAL_ACCESS_TOKEN, "
            "GITHUB_VON_TOKEN, GITHUB_TOKEN, or GH_TOKEN."
        )

    # Populate common token keys used by GitHub MCP server variants.
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    env["GH_TOKEN"] = token
    return env


def _build_github_config() -> GitHubProxyConfig:
    command = str(os.getenv("VON_GITHUB_MCP_COMMAND") or "npx").strip() or "npx"
    raw_args = os.getenv("VON_GITHUB_MCP_ARGS")
    if isinstance(raw_args, str) and raw_args.strip():
        args = shlex.split(raw_args.strip())
    else:
        args = ["-y", "@modelcontextprotocol/server-github"]
    if not args:
        raise GitHubProxyError("GitHub MCP args cannot be empty")
    return GitHubProxyConfig(
        command=command,
        args=args,
        env=_build_github_env(),
    )


_proxy_instance: Optional[GitHubMCPProxy] = None
_proxy_lock = asyncio.Lock()


async def get_github_proxy() -> GitHubMCPProxy:
    """Get or create the global GitHub MCP proxy instance."""
    global _proxy_instance

    async with _proxy_lock:
        if _proxy_instance is None:
            config = _build_github_config()
            _proxy_instance = GitHubMCPProxy(config)
            logger.info(
                "%s Initialised GitHub MCP proxy via command=%s args=%s",
                _LOG_TAG,
                config.command,
                config.args,
            )
        return _proxy_instance
