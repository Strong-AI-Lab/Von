"""LinkedIn Data Dump MCP proxy using the shared stdio client helper.

This proxy bridges a local LinkedIn MCP server so Von's internal MCP catalogue
can expose deterministic, typed tools for LinkedIn export inspection.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError

logger = logging.getLogger(__name__)
_LOG_TAG = "[linkedin_proxy]"

_DEFAULT_SERVER_PATH = Path("LinkedInMCP") / "server.py"
_DEFAULT_PYTHONPATH = _DEFAULT_SERVER_PATH.parent
_DEFAULT_DATA_ROOT = Path("data") / "linkedin"


class LinkedInProxyError(Exception):
    """Raised when LinkedIn MCP proxy operations fail."""


@dataclass
class LinkedInProxyConfig:
    """Configuration for LinkedIn MCP subprocess."""

    command: str
    args: list[str]
    env: Dict[str, str]
    data_root: Path
    timeout_sec: float = 30.0


def _coerce_value_from_text_payload(value: Any) -> Any:
    """Normalise MCP text payload wrappers into structured values when possible."""

    if isinstance(value, dict):
        text_payload = value.get("text")
        if isinstance(text_payload, str):
            stripped = text_payload.strip()
            if stripped:
                try:
                    return json.loads(stripped)
                except Exception:
                    pass
                try:
                    parsed_literal = ast.literal_eval(stripped)
                    if isinstance(parsed_literal, (dict, list, str, int, float, bool)):
                        return parsed_literal
                except Exception:
                    pass
        return value
    return value


def _coerce_list_payload(value: Any) -> list[str]:
    payload = _coerce_value_from_text_payload(value)
    if isinstance(payload, list):
        return [str(item) for item in payload]
    if isinstance(payload, dict):
        raw_items = payload.get("files") or payload.get("exports") or payload.get("items")
        if isinstance(raw_items, list):
            return [str(item) for item in raw_items]
    return []


def _coerce_mapping_payload(value: Any) -> dict[str, Any]:
    payload = _coerce_value_from_text_payload(value)
    if isinstance(payload, dict):
        return payload
    return {}


def _coerce_text_payload(value: Any) -> str:
    payload = _coerce_value_from_text_payload(value)
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(payload, ensure_ascii=True, default=str)
    if isinstance(payload, list):
        return "\n".join(str(item) for item in payload)
    return str(payload)


class LinkedInMCPProxy:
    """Manages local LinkedIn MCP server subprocess via shared MCP stdio client."""

    def __init__(self, config: LinkedInProxyConfig):
        self._config = config
        self._client = MCPStdIOClient(
            MCPServerConfig(
                command=config.command,
                args=config.args,
                env=config.env,
                timeout_sec=config.timeout_sec,
                log_tag=_LOG_TAG,
            )
        )

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await self._client.call_tool(tool_name, arguments)
        except MCPToolClientError as exc:
            raise LinkedInProxyError(str(exc)) from exc

    def _list_exports_from_data_root(self) -> list[str]:
        root = self._config.data_root
        if not root.exists() or not root.is_dir():
            return []

        exports: list[str] = []
        for child in root.iterdir():
            name = child.name
            if child.is_dir() or name.endswith(".zip") or name.endswith(".zip.zip"):
                exports.append(name)
        return sorted(exports, key=str.lower)

    async def list_exports(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            logger.info("%s Refresh requested for local export listing", _LOG_TAG)

        exports = self._list_exports_from_data_root()
        return {
            "success": True,
            "exports": exports,
            "total_exports": len(exports),
            "data_root": str(self._config.data_root),
            "data_root_exists": self._config.data_root.exists(),
        }

    async def list_files(self, *, export_name: str) -> dict[str, Any]:
        raw = await self._call("list_files", {"export_name": export_name})
        return {
            "success": True,
            "export_name": export_name,
            "files": _coerce_list_payload(raw),
        }

    async def get_profile(self, *, export_name: str) -> dict[str, Any]:
        raw = await self._call("get_profile", {"export_name": export_name})
        return {
            "success": True,
            "export_name": export_name,
            "profile": _coerce_text_payload(raw),
        }

    async def get_csv_data(
        self, *, export_name: str, file_name: str, limit: int = 10
    ) -> dict[str, Any]:
        raw = await self._call(
            "get_csv_data",
            {"export_name": export_name, "file_name": file_name, "limit": limit},
        )
        return {
            "success": True,
            "export_name": export_name,
            "file_name": file_name,
            "limit": limit,
            "data": _coerce_text_payload(raw),
        }

    async def get_company_stats(
        self, *, export_name: str, top_n: int = 10
    ) -> dict[str, Any]:
        raw = await self._call(
            "get_company_stats", {"export_name": export_name, "top_n": top_n}
        )
        stats_payload = _coerce_mapping_payload(raw)
        company_stats = stats_payload.get("company_stats")
        if not isinstance(company_stats, dict):
            company_stats = stats_payload if isinstance(stats_payload, dict) else {}
        return {
            "success": True,
            "export_name": export_name,
            "top_n": top_n,
            "company_stats": company_stats,
        }

    async def get_messages(self, *, export_name: str, query: str = "") -> dict[str, Any]:
        raw = await self._call(
            "get_messages", {"export_name": export_name, "query": query}
        )
        return {
            "success": True,
            "export_name": export_name,
            "query": query,
            "messages": _coerce_text_payload(raw),
        }

    def get_stats(self) -> dict[str, int]:
        return {
            "call_count": self._client.call_count,
            "error_count": self._client.error_count,
        }


def _compose_pythonpath(default_path: Path, existing: str | None) -> str:
    parts = [str(default_path)]
    if isinstance(existing, str) and existing.strip():
        parts.extend(
            part.strip()
            for part in existing.split(os.pathsep)
            if isinstance(part, str) and part.strip()
        )
    deduped: list[str] = []
    for part in parts:
        if part not in deduped:
            deduped.append(part)
    return os.pathsep.join(deduped)


def _build_linkedin_config() -> LinkedInProxyConfig:
    env = os.environ.copy()

    server_path_raw = (
        os.environ.get("VON_LINKEDIN_MCP_SERVER_PATH") or str(_DEFAULT_SERVER_PATH)
    )
    server_path = Path(server_path_raw).expanduser()
    if not server_path.exists():
        raise LinkedInProxyError(
            f"LinkedIn MCP server script not found at {server_path}. "
            "Set VON_LINKEDIN_MCP_SERVER_PATH to the correct server.py path."
        )

    command = os.environ.get("VON_LINKEDIN_MCP_COMMAND", "python").strip() or "python"
    args = [str(server_path)]

    pythonpath_override = os.environ.get("VON_LINKEDIN_MCP_PYTHONPATH")
    pythonpath_value = (
        pythonpath_override.strip()
        if isinstance(pythonpath_override, str) and pythonpath_override.strip()
        else str(_DEFAULT_PYTHONPATH)
    )
    env["PYTHONPATH"] = _compose_pythonpath(
        Path(pythonpath_value), env.get("PYTHONPATH")
    )

    data_root_raw = os.environ.get("VON_LINKEDIN_DATA_ROOT") or str(_DEFAULT_DATA_ROOT)
    data_root = Path(data_root_raw).expanduser()

    timeout_raw = os.environ.get("VON_LINKEDIN_MCP_TIMEOUT_SEC")
    try:
        timeout_sec = float(timeout_raw) if timeout_raw is not None else 30.0
    except Exception:
        timeout_sec = 30.0
    timeout_sec = max(5.0, min(300.0, timeout_sec))

    return LinkedInProxyConfig(
        command=command,
        args=args,
        env=env,
        data_root=data_root,
        timeout_sec=timeout_sec,
    )


_proxy_instance: Optional[LinkedInMCPProxy] = None
_proxy_lock = asyncio.Lock()


async def get_linkedin_proxy() -> LinkedInMCPProxy:
    """Get or create the singleton LinkedIn MCP proxy instance."""

    global _proxy_instance

    async with _proxy_lock:
        if _proxy_instance is None:
            config = _build_linkedin_config()
            _proxy_instance = LinkedInMCPProxy(config)
            logger.info(
                "%s Initialised LinkedIn MCP proxy using %s %s",
                _LOG_TAG,
                config.command,
                " ".join(config.args),
            )
        return _proxy_instance
