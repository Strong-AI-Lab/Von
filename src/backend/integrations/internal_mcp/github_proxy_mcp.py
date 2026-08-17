"""GitHub MCP proxy using the shared stdio client helper.

This bridges an external GitHub MCP server so Von's internal MCP gateway can
invoke GitHub tools through a single managed stdio client.
"""

from __future__ import annotations

import logging
import os
import shlex
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError
from ...utils.runtime_env import clean_env_value, read_repo_dotenv_values

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
_GITHUB_SUBPROCESS_PASSTHROUGH_KEYS: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
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


def _github_source_env() -> Dict[str, str]:
    dotenv_values = read_repo_dotenv_values(GITHUB_PROXY_ENV_OVERRIDE_KEYS)
    if dotenv_values:
        logger.info(
            "%s Loaded repo-root .env values for %s GitHub subprocess key(s).",
            _LOG_TAG,
            len(dotenv_values),
        )

    env = os.environ.copy()
    env.update(dotenv_values)
    return env


def _build_github_env(
    source_env: Mapping[str, str] | None = None,
) -> Dict[str, str]:
    resolved_source = (
        dict(source_env) if source_env is not None else _github_source_env()
    )

    # Resolve token from env vars in priority order and log which key was used.
    resolved_key, token = resolve_github_token(resolved_source)

    if not token:
        raise GitHubProxyError(
            "Missing GitHub token. Set one of: GITHUB_PERSONAL_ACCESS_TOKEN, "
            "GITHUB_VON_TOKEN, GITHUB_TOKEN, or GH_TOKEN."
        )

    logger.info(
        "%s Token resolved from %s (length=%d)",
        _LOG_TAG,
        resolved_key,
        len(token),
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

    runtime_root = Path(tempfile.gettempdir()) / "von-github-mcp"
    runtime_home = runtime_root / "home"
    runtime_cache = runtime_root / "cache"
    runtime_tmp = runtime_root / "tmp"
    npm_cache = runtime_root / "npm-cache"
    for path in (runtime_home, runtime_cache, runtime_tmp, npm_cache):
        path.mkdir(parents=True, exist_ok=True)

    env = {
        key: value
        for key in _GITHUB_SUBPROCESS_PASSTHROUGH_KEYS
        if (value := resolved_source.get(key))
    }
    env.update(
        {
            "HOME": str(runtime_home),
            "USERPROFILE": str(runtime_home),
            "XDG_CACHE_HOME": str(runtime_cache),
            "TMPDIR": str(runtime_tmp),
            "TEMP": str(runtime_tmp),
            "TMP": str(runtime_tmp),
            "NPM_CONFIG_CACHE": str(npm_cache),
        }
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
    source_env = _github_source_env()
    env = _build_github_env(source_env)
    command = (
        str(source_env.get("VON_GITHUB_MCP_COMMAND") or "npx").strip() or "npx"
    )
    raw_args = source_env.get("VON_GITHUB_MCP_ARGS")
    if isinstance(raw_args, str) and raw_args.strip():
        args = shlex.split(raw_args.strip())
    else:
        args = ["-y", "@modelcontextprotocol/server-github"]
    if not args:
        raise GitHubProxyError("GitHub MCP args cannot be empty")
    return GitHubProxyConfig(
        command=command,
        args=args,
        env=env,
    )


_proxy_instance: Optional[GitHubMCPProxy] = None
# Catalogue calls can run on fresh event loops, so construction is loop-neutral.
_proxy_lock = threading.Lock()


async def get_github_proxy() -> GitHubMCPProxy:
    """Get or create the global GitHub MCP proxy instance."""
    global _proxy_instance

    with _proxy_lock:
        if _proxy_instance is None:
            config = _build_github_config()
            _proxy_instance = GitHubMCPProxy(config)
            logger.info(
                "%s Initialised GitHub MCP proxy via command=%s arg_count=%d",
                _LOG_TAG,
                config.command,
                len(config.args),
            )
        return _proxy_instance
