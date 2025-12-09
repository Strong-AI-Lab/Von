"""Shared MCP stdio proxy utilities.

Provides a thin wrapper around the MCP SDK stdio client so internal proxies
can call external MCP servers consistently without duplicating connection
logic or response parsing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp import types as mcp_types

logger = logging.getLogger(__name__)


class MCPToolClientError(Exception):
    """Raised when an MCP tool invocation fails."""


@dataclass
class MCPServerConfig:
    """Configuration for an MCP stdio server process."""

    command: str
    args: list[str]
    env: Optional[Dict[str, str]] = None
    timeout_sec: float = 30.0
    log_tag: str = "[mcp_proxy]"


class MCPStdIOClient:
    """Lightweight stdio client for calling MCP tools.

    This helper hides the session setup/teardown boilerplate and provides
    basic parsing for common MCP content types. A custom ``text_parser`` can
    be supplied when a server returns structured text that needs special
    handling (e.g., Tavily's formatted responses).
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._call_count = 0
        self._error_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def error_count(self) -> int:
        return self._error_count

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        text_parser: Optional[Callable[[str], Any]] = None,
    ) -> Any:
        """Invoke an MCP tool and return the parsed payload.

        Args:
            tool_name: Name of the MCP tool to call.
            arguments: Arguments to pass to the tool.
            text_parser: Optional callable to post-process ``TextContent``.

        Returns:
            Parsed payload returned by the tool. Falls back to a dictionary
            containing raw text/image/resource content when structured data
            cannot be inferred.

        Raises:
            MCPToolClientError: When the tool invocation fails.
        """

        params = StdioServerParameters(
            command=self._config.command,
            args=self._config.args,
            env=self._config.env,
        )

        logger.info(
            "%s Calling tool %s with arguments %s",
            self._config.log_tag,
            tool_name,
            arguments,
        )

        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    self._call_count += 1
                    return self._parse_result(result, text_parser=text_parser)
        except Exception as exc:  # pragma: no cover - MCP failures are environment dependent
            self._error_count += 1
            logger.error("%s Tool call failed: %s", self._config.log_tag, exc)
            raise MCPToolClientError(str(exc)) from exc

    def _parse_result(
        self,
        result: Any,
        *,
        text_parser: Optional[Callable[[str], Any]] = None,
    ) -> Any:
        if result and getattr(result, "content", None):
            for item in result.content:
                if isinstance(item, mcp_types.TextContent):
                    text_data = item.text
                    if text_parser is not None:
                        try:
                            parsed = text_parser(text_data)
                            if parsed is not None:
                                return parsed
                        except Exception as parser_exc:  # pragma: no cover - defensive
                            logger.warning(
                                "%s text parser failed: %s",
                                self._config.log_tag,
                                parser_exc,
                            )
                    parsed_json = self._try_parse_json(text_data)
                    if parsed_json is not None:
                        return parsed_json
                    return {"text": text_data}
                if isinstance(item, mcp_types.ImageContent):
                    return {"image": item.data, "mimeType": item.mimeType}
                if isinstance(item, mcp_types.EmbeddedResource):
                    return {"resource": item.resource, "type": item.type}
        return {}

    @staticmethod
    def _try_parse_json(text_data: str) -> Any:
        stripped = text_data.strip()
        if not stripped:
            return None
        if stripped[0] not in "[{":
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None
