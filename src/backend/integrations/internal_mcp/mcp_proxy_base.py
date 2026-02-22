"""Shared MCP stdio proxy utilities.

Provides a thin wrapper around the MCP SDK stdio client so internal proxies
can call external MCP servers consistently without duplicating connection
logic or response parsing.
"""

from __future__ import annotations

import json
import logging
import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp import types as mcp_types

logger = logging.getLogger(__name__)


@dataclass
class MCPCallTelemetry:
    """Telemetry for an MCP tool call."""

    tool_name: str
    start_time_utc: str
    duration_ms: float
    success: bool
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    response_content_types: List[str] = field(default_factory=list)
    response_char_count: int = 0
    parsed_as: Optional[str] = None  # 'json', 'text', 'custom_parser', etc.

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "tool_name": self.tool_name,
            "start_time_utc": self.start_time_utc,
            "duration_ms": round(self.duration_ms, 2),
            "success": self.success,
        }
        if self.error_type:
            d["error_type"] = self.error_type
        if self.error_message:
            d["error_message"] = self.error_message
        if self.response_content_types:
            d["response_content_types"] = self.response_content_types
        if self.response_char_count:
            d["response_char_count"] = self.response_char_count
        if self.parsed_as:
            d["parsed_as"] = self.parsed_as
        return d


class MCPToolClientError(Exception):
    """Raised when an MCP tool invocation fails."""


@dataclass
class MCPServerConfig:
    """Configuration for an MCP stdio server process."""

    command: str
    args: list[str]
    env: Optional[Dict[str, str]] = None
    timeout_sec: float = 30.0
    # Bounded transport recovery for transient stdio/session drops.
    transport_retry_attempts: int = 1
    transport_retry_backoff_sec: float = 0.2
    log_tag: str = "[mcp_proxy]"


class MCPStdIOClient:
    """Lightweight stdio client for calling MCP tools.

    This helper hides the session setup/teardown boilerplate and provides
    basic parsing for common MCP content types. A custom ``text_parser`` can
    be supplied when a server returns structured text that needs special
    handling (e.g., Tavily's formatted responses).
    """

    # Maximum telemetry entries to retain (ring buffer)
    _MAX_TELEMETRY_HISTORY = 50

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._call_count = 0
        self._error_count = 0
        self._total_duration_ms = 0.0
        self._last_call_telemetry: Optional[MCPCallTelemetry] = None
        self._telemetry_history: List[MCPCallTelemetry] = []

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def total_duration_ms(self) -> float:
        return self._total_duration_ms

    @property
    def last_call_telemetry(self) -> Optional[MCPCallTelemetry]:
        return self._last_call_telemetry

    @property
    def telemetry_history(self) -> List[MCPCallTelemetry]:
        return list(self._telemetry_history)

    def _record_telemetry(self, telemetry: MCPCallTelemetry) -> None:
        """Record telemetry entry, maintaining ring buffer."""
        self._last_call_telemetry = telemetry
        self._telemetry_history.append(telemetry)
        if len(self._telemetry_history) > self._MAX_TELEMETRY_HISTORY:
            self._telemetry_history.pop(0)

    @staticmethod
    def _collect_exception_text(exc: BaseException) -> str:
        """Flatten exception and chained causes into a searchable message blob."""
        segments: list[str] = []
        seen: set[int] = set()

        def _walk(err: BaseException | None) -> None:
            if err is None:
                return
            err_id = id(err)
            if err_id in seen:
                return
            seen.add(err_id)

            message = str(err).strip()
            if message:
                segments.append(message)

            nested = getattr(err, "exceptions", None)
            if isinstance(nested, (list, tuple)):
                for sub_err in nested:
                    if isinstance(sub_err, BaseException):
                        _walk(sub_err)

            cause = getattr(err, "__cause__", None)
            if isinstance(cause, BaseException):
                _walk(cause)
            context = getattr(err, "__context__", None)
            if isinstance(context, BaseException):
                _walk(context)

        _walk(exc)
        return " | ".join(segments)

    @classmethod
    def _is_transport_closed_error(cls, exc: BaseException) -> bool:
        """Return True when error text indicates a dropped MCP transport/session."""
        flattened = cls._collect_exception_text(exc).lower()
        if not flattened:
            return False
        transport_tokens = (
            "transport closed",
            "connection closed",
            "stream closed",
            "broken pipe",
            "connection reset by peer",
            "eof",
            "closed resource",
        )
        return any(token in flattened for token in transport_tokens)

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
        from datetime import datetime, timezone

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

        start_time = time.perf_counter()
        start_time_utc = datetime.now(timezone.utc).isoformat()
        telemetry = MCPCallTelemetry(
            tool_name=tool_name,
            start_time_utc=start_time_utc,
            duration_ms=0.0,
            success=False,
        )

        max_retries = max(0, int(self._config.transport_retry_attempts))
        attempt = 0
        try:
            while True:
                try:
                    async with stdio_client(params) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            result = await session.call_tool(tool_name, arguments)
                            duration_ms = (time.perf_counter() - start_time) * 1000
                            self._call_count += 1
                            self._total_duration_ms += duration_ms

                            # Extract content types for telemetry
                            content_types = []
                            char_count = 0
                            if result and getattr(result, "content", None):
                                for item in result.content:
                                    if isinstance(item, mcp_types.TextContent):
                                        content_types.append("text")
                                        char_count += len(item.text or "")
                                    elif isinstance(item, mcp_types.ImageContent):
                                        content_types.append("image")
                                    elif isinstance(item, mcp_types.EmbeddedResource):
                                        content_types.append("resource")
                                    else:
                                        content_types.append(type(item).__name__)

                            parsed, parsed_as = self._parse_result_with_telemetry(
                                result, text_parser=text_parser
                            )

                            telemetry.duration_ms = duration_ms
                            telemetry.success = True
                            telemetry.response_content_types = content_types
                            telemetry.response_char_count = char_count
                            telemetry.parsed_as = parsed_as
                            self._record_telemetry(telemetry)

                            logger.info(
                                "%s Tool %s completed in %.1fms (chars=%d, parsed_as=%s)",
                                self._config.log_tag,
                                tool_name,
                                duration_ms,
                                char_count,
                                parsed_as,
                            )

                            return parsed
                except Exception as exc:
                    should_retry = self._is_transport_closed_error(exc) and attempt < max_retries
                    if should_retry:
                        attempt += 1
                        backoff = max(
                            0.0, float(self._config.transport_retry_backoff_sec)
                        ) * attempt
                        logger.warning(
                            "%s Tool %s transport drop detected (%s); retry %d/%d in %.2fs",
                            self._config.log_tag,
                            tool_name,
                            type(exc).__name__,
                            attempt,
                            max_retries,
                            backoff,
                        )
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        continue
                    raise
        except Exception as exc:  # pragma: no cover - MCP failures are environment dependent
            duration_ms = (time.perf_counter() - start_time) * 1000
            self._error_count += 1
            self._total_duration_ms += duration_ms

            telemetry.duration_ms = duration_ms
            telemetry.error_type = type(exc).__name__
            telemetry.error_message = str(exc)[:500]  # Truncate long errors
            self._record_telemetry(telemetry)

            logger.error(
                "%s Tool %s failed after %.1fms: [%s] %s",
                self._config.log_tag,
                tool_name,
                duration_ms,
                type(exc).__name__,
                exc,
            )
            raise MCPToolClientError(str(exc)) from exc

    async def list_tools(self) -> list[Dict[str, Any]]:
        """List tools exposed by the MCP server."""

        params = StdioServerParameters(
            command=self._config.command,
            args=self._config.args,
            env=self._config.env,
        )

        logger.info("%s Listing tools", self._config.log_tag)

        max_retries = max(0, int(self._config.transport_retry_attempts))
        attempt = 0
        try:
            while True:
                try:
                    async with stdio_client(params) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            result = await session.list_tools()
                            self._call_count += 1
                            tools = self._extract_tools(result)
                            return [self._serialise_tool(tool) for tool in tools]
                except Exception as exc:
                    should_retry = self._is_transport_closed_error(exc) and attempt < max_retries
                    if should_retry:
                        attempt += 1
                        backoff = max(
                            0.0, float(self._config.transport_retry_backoff_sec)
                        ) * attempt
                        logger.warning(
                            "%s list_tools transport drop detected (%s); retry %d/%d in %.2fs",
                            self._config.log_tag,
                            type(exc).__name__,
                            attempt,
                            max_retries,
                            backoff,
                        )
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        continue
                    raise exc
        except Exception as exc:  # pragma: no cover - environment dependent
            self._error_count += 1
            logger.error("%s Tool list failed: %s", self._config.log_tag, exc)
            raise MCPToolClientError(str(exc)) from exc

    def _parse_result(
        self,
        result: Any,
        *,
        text_parser: Optional[Callable[[str], Any]] = None,
    ) -> Any:
        """Parse result without telemetry info (backward compat)."""
        parsed, _ = self._parse_result_with_telemetry(result, text_parser=text_parser)
        return parsed

    def _parse_result_with_telemetry(
        self,
        result: Any,
        *,
        text_parser: Optional[Callable[[str], Any]] = None,
    ) -> tuple[Any, Optional[str]]:
        """Parse result and return (parsed_data, parse_method) for telemetry."""
        if result and getattr(result, "content", None):
            for item in result.content:
                if isinstance(item, mcp_types.TextContent):
                    text_data = item.text
                    if text_parser is not None:
                        try:
                            parsed = text_parser(text_data)
                            if parsed is not None:
                                return parsed, "custom_parser"
                        except Exception as parser_exc:  # pragma: no cover - defensive
                            logger.warning(
                                "%s text parser failed: %s",
                                self._config.log_tag,
                                parser_exc,
                            )
                    parsed_json = self._try_parse_json(text_data)
                    if parsed_json is not None:
                        return parsed_json, "json"
                    return {"text": text_data}, "raw_text"
                if isinstance(item, mcp_types.ImageContent):
                    return {"image": item.data, "mimeType": item.mimeType}, "image"
                if isinstance(item, mcp_types.EmbeddedResource):
                    return {"resource": item.resource, "type": item.type}, "resource"
        return {}, "empty"

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

    @staticmethod
    def _extract_tools(result: Any) -> Sequence[Any]:
        tools = getattr(result, "tools", None)
        if tools is not None:
            return tools
        if isinstance(result, list):
            return result
        return ()

    @staticmethod
    def _serialise_tool(tool: Any) -> Dict[str, Any]:
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", None)
        input_schema = getattr(tool, "inputSchema", None)
        if isinstance(tool, dict):
            name = tool.get("name", name)
            description = tool.get("description", description)
            input_schema = tool.get("inputSchema", input_schema)
        payload: Dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if input_schema is not None:
            payload["input_schema"] = input_schema
        return payload
