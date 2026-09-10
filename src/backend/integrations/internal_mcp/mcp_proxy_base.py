"""Shared MCP stdio proxy utilities.

Provides a thin wrapper around the MCP SDK stdio client so internal proxies
can call external MCP servers consistently without duplicating connection
logic or response parsing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence
from uuid import uuid4

from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)


def summarise_mcp_tool_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Describe argument structure for logs without copying argument values."""

    shapes: Dict[str, str] = {}
    keys = sorted(str(key)[:80] for key in arguments)[:32]
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str):
            shapes[key] = f"string:{len(value)}"
        elif isinstance(value, (bytes, bytearray)):
            shapes[key] = f"bytes:{len(value)}"
        elif isinstance(value, dict):
            shapes[key] = f"object:{len(value)}"
        elif isinstance(value, (list, tuple, set)):
            shapes[key] = f"array:{len(value)}"
        elif value is None:
            shapes[key] = "null"
        else:
            shapes[key] = type(value).__name__
    return {
        "argument_count": len(arguments),
        "keys": keys,
        "shapes": shapes,
        "keys_truncated": len(arguments) > len(keys),
    }


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


class _MCPToolResultError(RuntimeError):
    """Internal error raised when an MCP server returns ``isError=true``."""

    def __init__(self, *, tool_name: str, actor_message: str) -> None:
        self.actor_message = actor_message
        super().__init__(f"MCP tool {tool_name} reported an error")


@dataclass
class MCPServerConfig:
    """Configuration for an MCP stdio server process."""

    command: str
    args: list[str]
    env: Optional[Dict[str, str]] = None
    timeout_sec: float = 30.0
    # Reserve part of the caller/configured budget so the MCP SDK normally has
    # time to terminate the stdio child after an operation is cancelled.
    shutdown_grace_sec: float = 2.5
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
        self._helper_owner_token = (config.env or {}).get(
            "VON_MCP_HELPER_OWNER_TOKEN"
        ) or f"proxy-owner:{uuid4()}"
        self._call_count = 0
        self._error_count = 0
        self._total_duration_ms = 0.0
        self._last_call_telemetry: Optional[MCPCallTelemetry] = None
        self._telemetry_history: List[MCPCallTelemetry] = []
        self._capture_session: ContextVar[dict[str, Any] | None] = ContextVar(
            f"mcp_capture_session_{id(self)}", default=None
        )

    @asynccontextmanager
    async def _new_session(self, params):
        # Bound startup independently; borrowed calls retain their own existing
        # operation deadlines and telemetry below.
        async with asyncio.timeout(self._effective_operation_timeout_sec()) as startup:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    startup.reschedule(None)
                    yield session

    @asynccontextmanager
    async def reuse_session(self):
        """Reuse one helper in an explicitly bounded caller scope, then close it.

        Context-local state cannot lend a session to an unrelated caller. A
        dropped transport invalidates the borrowed session; normal per-call
        retries then open fresh helpers. This is not a persistent process pool.
        """
        existing = self._capture_session.get()
        if existing and existing["usable"]:
            yield
            return
        state = None
        token = None
        body_failed = False
        try:
            async with self._new_session(self._build_server_params()) as session:
                state = {"session": session, "usable": True}
                token = self._capture_session.set(state)
                try:
                    yield
                except BaseException:
                    body_failed = True
                    raise
        except Exception as exc:
            # A failed borrowed transport can also report its close while the
            # scope unwinds. Preserve successfully recovered per-call results;
            # never suppress a caller failure or an unrelated close error.
            if (
                body_failed
                or not state
                or state["usable"]
                or not self._is_transport_closed_error(exc)
            ):
                raise
        finally:
            if token is not None:
                self._capture_session.reset(token)

    @asynccontextmanager
    async def _call_session(self, params):
        state = self._capture_session.get()
        if state and state["usable"]:
            yield state["session"]
        else:
            async with self._new_session(params) as session:
                yield session

    @property
    def helper_owner_token(self) -> str:
        return self._helper_owner_token

    def _build_server_params(self) -> StdioServerParameters:
        env = dict(self._config.env or {})
        env.setdefault("VON_MCP_HELPER_OWNER_TOKEN", self._helper_owner_token)
        env.setdefault("VON_MCP_HELPER_OWNER_LABEL", self._config.log_tag)
        env.setdefault("VON_MCP_HELPER_PARENT_PID", str(os.getpid()))
        return StdioServerParameters(
            command=self._config.command,
            args=self._config.args,
            env=env,
        )

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

    def _effective_operation_timeout_sec(self) -> float:
        """Return a deadline that fits within both config and active caller scope.

        The internal MCP gateway cannot forcibly stop a worker thread. Its
        execution scope is therefore propagated into stdio clients so an
        external MCP process is asked to stop with some caller budget remaining.
        The reserve gives normal SDK cleanup an opportunity; it is not a hard
        guarantee against cleanup code that suppresses or shields cancellation.
        """

        try:
            configured_timeout = float(self._config.timeout_sec)
        except (TypeError, ValueError):
            configured_timeout = 30.0
        configured_timeout = max(0.001, configured_timeout)
        total_budget = configured_timeout

        active_scope = None
        try:
            # Lazy import avoids coupling the shared MCP client to gateway
            # initialisation while still using its request-local deadline.
            from .transport import get_internal_mcp_execution_scope

            active_scope = get_internal_mcp_execution_scope()
        except (ImportError, AttributeError):  # pragma: no cover - defensive boundary
            active_scope = None

        if active_scope is not None:
            if active_scope.cancellation_requested:
                return 0.0
            total_budget = min(total_budget, active_scope.remaining_seconds)
            if total_budget <= 0.01:
                return 0.0

        try:
            configured_grace = max(0.0, float(self._config.shutdown_grace_sec))
        except (TypeError, ValueError):
            configured_grace = 2.5
        # Do not consume a tiny configured timeout entirely with cleanup reserve.
        cleanup_grace = min(configured_grace, total_budget * 0.2)
        return max(0.001, total_budget - cleanup_grace)

    @staticmethod
    def _tool_result_error_message(result: Any, *, tool_name: str) -> str:
        """Extract a bounded human-readable message from an MCP error result."""

        messages: list[str] = []
        for item in list(getattr(result, "content", None) or []):
            if isinstance(item, mcp_types.TextContent):
                text_value = str(item.text or "").strip()
                if text_value:
                    messages.append(text_value)

        structured_content = getattr(result, "structuredContent", None)
        if not messages and isinstance(structured_content, dict):
            try:
                messages.append(json.dumps(structured_content, ensure_ascii=True))
            except (TypeError, ValueError):
                pass

        message = " | ".join(dict.fromkeys(messages)).strip()
        if not message:
            message = f"MCP tool {tool_name} reported an error"
        return message[:2000]

    @staticmethod
    def _walk_exception_messages(exc: BaseException) -> tuple[list[str], list[str]]:
        """Return flattened exception text and leaf-only exception text."""

        messages: list[str] = []
        leaf_messages: list[str] = []
        seen: set[int] = set()

        def _walk(err: BaseException | None) -> None:
            if err is None:
                return
            err_id = id(err)
            if err_id in seen:
                return
            seen.add(err_id)

            message = str(err).strip()
            nested_errors: list[BaseException] = []
            nested = getattr(err, "exceptions", None)
            if isinstance(nested, (list, tuple)):
                for sub_err in nested:
                    if isinstance(sub_err, BaseException):
                        nested_errors.append(sub_err)

            cause = getattr(err, "__cause__", None)
            if isinstance(cause, BaseException):
                nested_errors.append(cause)
            context = getattr(err, "__context__", None)
            if isinstance(context, BaseException) and context is not cause:
                nested_errors.append(context)

            if message:
                messages.append(message)

            if nested_errors:
                for nested_error in nested_errors:
                    _walk(nested_error)
                return

            if message:
                leaf_messages.append(message)

        _walk(exc)
        return messages, leaf_messages

    @classmethod
    def _collect_exception_text(cls, exc: BaseException) -> str:
        """Flatten exception and chained causes into a searchable message blob."""

        segments, _ = cls._walk_exception_messages(exc)
        return " | ".join(segments)

    @classmethod
    def _collect_leaf_exception_text(cls, exc: BaseException) -> str:
        """Return the leaf-most exception text for user-facing error messages."""

        _, leaf_messages = cls._walk_exception_messages(exc)
        if leaf_messages:
            unique_leaf_messages: list[str] = []
            for message in leaf_messages:
                if message not in unique_leaf_messages:
                    unique_leaf_messages.append(message)
            return " | ".join(unique_leaf_messages)
        return cls._collect_exception_text(exc)

    @staticmethod
    def _select_leaf_exception(exc: BaseException) -> BaseException:
        """Prefer the first leaf exception so error types match the real failure."""

        seen: set[int] = set()

        def _walk(err: BaseException) -> BaseException:
            err_id = id(err)
            if err_id in seen:
                return err
            seen.add(err_id)

            nested = getattr(err, "exceptions", None)
            if isinstance(nested, (list, tuple)):
                for sub_err in nested:
                    if isinstance(sub_err, BaseException):
                        return _walk(sub_err)

            cause = getattr(err, "__cause__", None)
            if isinstance(cause, BaseException):
                return _walk(cause)

            context = getattr(err, "__context__", None)
            if isinstance(context, BaseException) and context is not cause:
                return _walk(context)

            return err

        return _walk(exc)

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

        params = self._build_server_params()

        logger.info(
            "%s Calling tool %s with argument_shape=%s",
            self._config.log_tag,
            tool_name,
            summarise_mcp_tool_arguments(arguments),
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
        operation_timeout_sec = self._effective_operation_timeout_sec()
        try:
            if operation_timeout_sec <= 0.0:
                raise TimeoutError(
                    f"MCP tool {tool_name} caller deadline was already exhausted"
                )
            async with asyncio.timeout(operation_timeout_sec):
                while True:
                    try:
                        async with self._call_session(params) as session:
                            result = await session.call_tool(tool_name, arguments)
                            if bool(getattr(result, "isError", False)):
                                raise _MCPToolResultError(
                                    tool_name=tool_name,
                                    actor_message=self._tool_result_error_message(
                                        result,
                                        tool_name=tool_name,
                                    ),
                                )
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
                        state = self._capture_session.get()
                        if state and (
                            isinstance(exc, TimeoutError)
                            or self._is_transport_closed_error(exc)
                        ):
                            state["usable"] = False
                        should_retry = (
                            self._is_transport_closed_error(exc)
                            and attempt < max_retries
                        )
                        if should_retry:
                            attempt += 1
                            backoff = (
                                max(
                                    0.0, float(self._config.transport_retry_backoff_sec)
                                )
                                * attempt
                            )
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
        except Exception as exc:  # pragma: no cover
            state = self._capture_session.get()
            if state and (
                isinstance(exc, TimeoutError) or self._is_transport_closed_error(exc)
            ):
                state["usable"] = False
            duration_ms = (time.perf_counter() - start_time) * 1000
            self._error_count += 1
            self._total_duration_ms += duration_ms

            root_cause = self._select_leaf_exception(exc)
            if isinstance(exc, TimeoutError):
                user_message = str(exc).strip() or (
                    f"MCP tool {tool_name} exceeded its "
                    f"{operation_timeout_sec:.3f}s operation deadline"
                )
                root_cause = exc
                telemetry_message = user_message
            elif isinstance(root_cause, _MCPToolResultError):
                user_message = root_cause.actor_message
                telemetry_message = str(root_cause)
            else:
                user_message = self._collect_leaf_exception_text(exc)
                telemetry_message = user_message
            flattened_error = self._collect_exception_text(exc)

            telemetry.duration_ms = duration_ms
            telemetry.error_type = type(root_cause).__name__
            telemetry.error_message = telemetry_message[:500]
            self._record_telemetry(telemetry)

            logger.error(
                "%s Tool %s failed after %.1fms: [%s] %s (root: [%s] %s, flattened=%s)",
                self._config.log_tag,
                tool_name,
                duration_ms,
                type(exc).__name__,
                exc,
                type(root_cause).__name__,
                root_cause,
                flattened_error,
            )
            raise MCPToolClientError(user_message or str(root_cause)) from exc

    async def list_tools(self) -> list[Dict[str, Any]]:
        """List tools exposed by the MCP server."""

        params = self._build_server_params()

        logger.info("%s Listing tools", self._config.log_tag)

        max_retries = max(0, int(self._config.transport_retry_attempts))
        attempt = 0
        operation_timeout_sec = self._effective_operation_timeout_sec()
        try:
            if operation_timeout_sec <= 0.0:
                raise TimeoutError(
                    "MCP tool-list caller deadline was already exhausted"
                )
            async with asyncio.timeout(operation_timeout_sec):
                while True:
                    try:
                        async with stdio_client(params) as (read_stream, write_stream):
                            async with ClientSession(
                                read_stream, write_stream
                            ) as session:
                                await session.initialize()
                                result = await session.list_tools()
                                self._call_count += 1
                                tools = self._extract_tools(result)
                                return [self._serialise_tool(tool) for tool in tools]
                    except Exception as exc:
                        should_retry = (
                            self._is_transport_closed_error(exc)
                            and attempt < max_retries
                        )
                        if should_retry:
                            attempt += 1
                            backoff = (
                                max(
                                    0.0, float(self._config.transport_retry_backoff_sec)
                                )
                                * attempt
                            )
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
                        raise
        except Exception as exc:  # pragma: no cover - environment dependent
            self._error_count += 1
            root_cause = self._select_leaf_exception(exc)
            if isinstance(exc, TimeoutError):
                user_message = str(exc).strip() or (
                    "MCP tool listing exceeded its "
                    f"{operation_timeout_sec:.3f}s operation deadline"
                )
                root_cause = exc
            else:
                user_message = self._collect_leaf_exception_text(exc)
            flattened_error = self._collect_exception_text(exc)
            logger.error(
                "%s Tool list failed: [%s] %s (root: [%s] %s, flattened=%s)",
                self._config.log_tag,
                type(exc).__name__,
                exc,
                type(root_cause).__name__,
                root_cause,
                flattened_error,
            )
            raise MCPToolClientError(user_message or str(root_cause)) from exc

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
