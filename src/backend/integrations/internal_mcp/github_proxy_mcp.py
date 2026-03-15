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
from typing import Any, Dict, Mapping, Optional

from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError
from ...utils.runtime_env import apply_repo_dotenv_overrides, clean_env_value

logger = logging.getLogger(__name__)
_LOG_TAG = "[github_proxy]"
GITHUB_TOKEN_ENV_KEYS: tuple[str, ...] = (
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GITHUB_VON_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)
GITHUB_PROXY_ENV_OVERRIDE_KEYS: tuple[str, ...] = (
    *GITHUB_TOKEN_ENV_KEYS,
    "VON_GITHUB_MCP_COMMAND",
    "VON_GITHUB_MCP_ARGS",
)


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
    applied_overrides = apply_repo_dotenv_overrides(GITHUB_PROXY_ENV_OVERRIDE_KEYS)
    if applied_overrides:
        logger.info(
            "%s Applied repo-root .env overrides for %s GitHub key(s).",
            _LOG_TAG,
            len(applied_overrides),
        )

    env = os.environ.copy()

    # Resolve token from env vars in priority order and log which key was used.
    resolved_key, token = resolve_github_token(env)

    if not token:
        raise GitHubProxyError(
            "Missing GitHub token. Set one of: GITHUB_PERSONAL_ACCESS_TOKEN, "
            "GITHUB_VON_TOKEN, GITHUB_TOKEN, or GH_TOKEN."
        )

    logger.info(
        "%s Token resolved from %s (length=%d, prefix=%s...)",
        _LOG_TAG,
        resolved_key,
        len(token),
        token[:12] if len(token) > 12 else "***",
    )

    # GITHUB_TOKEN / GH_TOKEN are low-priority fallbacks that VS Code may
    # have set to its own Copilot auth token (insufficient scopes for the
    # GitHub MCP server).  Warn so operators can add a proper PAT to .env.
    if resolved_key in ("GITHUB_TOKEN", "GH_TOKEN"):
        logger.warning(
            "%s Token resolved from %s — this may be a VS Code Copilot token "
            "with insufficient scopes. Set GITHUB_PERSONAL_ACCESS_TOKEN in "
            ".env for reliable GitHub API access.",
            _LOG_TAG,
            resolved_key,
        )

    # Populate common token keys used by GitHub MCP server variants.
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    env["GH_TOKEN"] = token
    return env


def resolve_github_token(
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the first configured GitHub token and the key it came from."""

    source_env = os.environ if env is None else env
    for candidate_key in GITHUB_TOKEN_ENV_KEYS:
        candidate_value = clean_env_value(source_env.get(candidate_key))
        if candidate_value:
            return candidate_key, candidate_value
    return None, None


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
