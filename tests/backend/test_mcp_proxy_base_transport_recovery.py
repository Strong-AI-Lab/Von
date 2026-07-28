from __future__ import annotations

import asyncio
import logging
import time
import types

import pytest
from mcp import types as mcp_types

import src.backend.integrations.internal_mcp.mcp_proxy_base as mcp_proxy_base
from src.backend.integrations.internal_mcp.mcp_proxy_base import (
    MCPServerConfig,
    MCPStdIOClient,
    MCPToolClientError,
    summarise_mcp_tool_arguments,
)


class _DummyStdioContext:
    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_call_tool_injects_stable_helper_owner_token_into_server_env(monkeypatch):
    captured_envs: list[dict[str, str]] = []

    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _tool_name, _arguments):
            return types.SimpleNamespace(
                content=[mcp_types.TextContent(type="text", text='{"success": true}')]
            )

    def _fake_stdio_client(params):
        captured_envs.append(dict(params.env or {}))
        return _DummyStdioContext()

    monkeypatch.setattr(mcp_proxy_base, "stdio_client", _fake_stdio_client)
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            env={"EXISTING_VAR": "1"},
            transport_retry_attempts=0,
            transport_retry_backoff_sec=0.0,
            log_tag="[unit-proxy]",
        )
    )

    asyncio.run(client.call_tool("fetch_concept", {"concept_id": "#V#x"}))
    asyncio.run(client.call_tool("fetch_concept", {"concept_id": "#V#y"}))

    assert len(captured_envs) == 2
    assert captured_envs[0]["EXISTING_VAR"] == "1"
    assert captured_envs[1]["EXISTING_VAR"] == "1"
    assert captured_envs[0]["VON_MCP_HELPER_OWNER_TOKEN"] == client.helper_owner_token
    assert captured_envs[1]["VON_MCP_HELPER_OWNER_TOKEN"] == client.helper_owner_token
    assert captured_envs[0]["VON_MCP_HELPER_OWNER_LABEL"] == "[unit-proxy]"
    assert captured_envs[0]["VON_MCP_HELPER_PARENT_PID"].isdigit()


def test_call_tool_retries_transport_closed_once_then_succeeds(monkeypatch):
    state = {"calls": 0}

    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _tool_name, _arguments):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("tools/call failed: Transport closed")
            return types.SimpleNamespace(
                content=[mcp_types.TextContent(type="text", text='{"success": true}')]
            )

    monkeypatch.setattr(
        mcp_proxy_base, "stdio_client", lambda _params: _DummyStdioContext()
    )
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            transport_retry_attempts=1,
            transport_retry_backoff_sec=0.0,
        )
    )

    result = asyncio.run(client.call_tool("fetch_concept", {"concept_id": "#V#x"}))
    assert result == {"success": True}
    assert state["calls"] == 2
    assert client.error_count == 0
    assert client.call_count == 1


def test_call_tool_does_not_retry_non_transport_error(monkeypatch):
    state = {"calls": 0}

    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _tool_name, _arguments):
            state["calls"] += 1
            raise RuntimeError("invalid request payload")

    monkeypatch.setattr(
        mcp_proxy_base, "stdio_client", lambda _params: _DummyStdioContext()
    )
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            transport_retry_attempts=2,
            transport_retry_backoff_sec=0.0,
        )
    )

    with pytest.raises(MCPToolClientError):
        asyncio.run(client.call_tool("fetch_concept", {"concept_id": "#V#x"}))

    assert state["calls"] == 1
    assert client.error_count == 1
    assert client.call_count == 0


def test_list_tools_retries_transport_closed_once_then_succeeds(monkeypatch):
    state = {"calls": 0}

    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("Transport closed while listing tools")
            return types.SimpleNamespace(
                tools=[{"name": "jira_search", "description": "Search Jira"}]
            )

    monkeypatch.setattr(
        mcp_proxy_base, "stdio_client", lambda _params: _DummyStdioContext()
    )
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            transport_retry_attempts=1,
            transport_retry_backoff_sec=0.0,
        )
    )

    tools = asyncio.run(client.list_tools())
    assert tools == [{"name": "jira_search", "description": "Search Jira"}]
    assert state["calls"] == 2
    assert client.error_count == 0
    assert client.call_count == 1


def test_call_tool_surfaces_leaf_exception_message_from_exception_group(monkeypatch):
    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _tool_name, _arguments):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [RuntimeError("Missing GitHub token from repo-root .env")],
            )

    monkeypatch.setattr(
        mcp_proxy_base, "stdio_client", lambda _params: _DummyStdioContext()
    )
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            transport_retry_attempts=0,
            transport_retry_backoff_sec=0.0,
        )
    )

    with pytest.raises(
        MCPToolClientError,
        match="Missing GitHub token from repo-root \\.env",
    ):
        asyncio.run(
            client.call_tool("github_get_file_contents", {"owner": "Strong-AI-Lab"})
        )

    assert client.error_count == 1
    assert client.call_count == 0
    assert client.last_call_telemetry is not None
    assert client.last_call_telemetry.error_type == "RuntimeError"
    assert (
        client.last_call_telemetry.error_message
        == "Missing GitHub token from repo-root .env"
    )


def test_list_tools_surfaces_leaf_exception_message_from_exception_group(monkeypatch):
    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [ConnectionRefusedError("connection refused")],
            )

    monkeypatch.setattr(
        mcp_proxy_base, "stdio_client", lambda _params: _DummyStdioContext()
    )
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            transport_retry_attempts=0,
            transport_retry_backoff_sec=0.0,
        )
    )

    with pytest.raises(MCPToolClientError, match="connection refused"):
        asyncio.run(client.list_tools())

    assert client.error_count == 1
    assert client.call_count == 0


def test_call_tool_treats_mcp_error_result_as_failure_without_logging_content(
    monkeypatch,
    caplog,
):
    private_marker = "PRIVATE-MCP-ERROR-MARKER-78c91"

    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _tool_name, _arguments):
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(
                        type="text",
                        text=(
                            "Tavily API error: certificate verification failed "
                            f"{private_marker}"
                        ),
                    )
                ],
                isError=True,
            )

    monkeypatch.setattr(
        mcp_proxy_base,
        "stdio_client",
        lambda _params: _DummyStdioContext(),
    )
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            transport_retry_attempts=0,
        )
    )

    with caplog.at_level(logging.ERROR, logger=mcp_proxy_base.__name__):
        with pytest.raises(MCPToolClientError) as raised:
            asyncio.run(client.call_tool("tavily_search", {"query": "test"}))

    assert private_marker in str(raised.value)
    assert private_marker not in caplog.text
    assert client.call_count == 0
    assert client.error_count == 1
    assert client.last_call_telemetry is not None
    assert client.last_call_telemetry.success is False
    assert client.last_call_telemetry.error_type == "_MCPToolResultError"
    assert (
        client.last_call_telemetry.error_message
        == "MCP tool tavily_search reported an error"
    )
    assert private_marker not in repr(client.last_call_telemetry.to_dict())


def test_call_tool_honours_configured_deadline_and_closes_stdio(monkeypatch):
    state = {"stdio_exited": False, "session_exited": False}

    class _TrackingStdioContext:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, exc_type, exc, tb):
            state["stdio_exited"] = True
            return False

    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            state["session_exited"] = True
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _tool_name, _arguments):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        mcp_proxy_base,
        "stdio_client",
        lambda _params: _TrackingStdioContext(),
    )
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            timeout_sec=0.05,
            shutdown_grace_sec=0.0,
            transport_retry_attempts=0,
        )
    )

    started = time.perf_counter()
    with pytest.raises(MCPToolClientError, match="operation deadline"):
        asyncio.run(client.call_tool("slow_tool", {}))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert state == {"stdio_exited": True, "session_exited": True}
    assert client.last_call_telemetry is not None
    assert client.last_call_telemetry.error_type == "TimeoutError"


def test_call_tool_uses_shorter_active_gateway_deadline(monkeypatch):
    from src.backend.integrations.internal_mcp import transport

    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _tool_name, _arguments):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        mcp_proxy_base,
        "stdio_client",
        lambda _params: _DummyStdioContext(),
    )
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)
    monkeypatch.setattr(
        transport,
        "get_internal_mcp_execution_scope",
        lambda: types.SimpleNamespace(
            cancellation_requested=False,
            remaining_seconds=0.04,
        ),
    )

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            timeout_sec=5.0,
            shutdown_grace_sec=0.0,
            transport_retry_attempts=0,
        )
    )

    started = time.perf_counter()
    with pytest.raises(MCPToolClientError, match="operation deadline"):
        asyncio.run(client.call_tool("slow_tool", {}))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5


def test_list_tools_honours_configured_deadline(monkeypatch):
    class _Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        mcp_proxy_base,
        "stdio_client",
        lambda _params: _DummyStdioContext(),
    )
    monkeypatch.setattr(mcp_proxy_base, "ClientSession", _Session)

    client = MCPStdIOClient(
        MCPServerConfig(
            command="python",
            args=["server.py"],
            timeout_sec=0.05,
            shutdown_grace_sec=0.0,
            transport_retry_attempts=0,
        )
    )

    with pytest.raises(MCPToolClientError, match="operation deadline"):
        asyncio.run(client.list_tools())

    assert client.call_count == 0
    assert client.error_count == 1


def test_argument_log_summary_does_not_copy_values() -> None:
    secret_query = "private research question"
    summary = summarise_mcp_tool_arguments(
        {
            "query": secret_query,
            "urls": ["https://private.example.test/document"],
            "options": {"token": "also-private"},
        }
    )

    rendered = repr(summary)
    assert secret_query not in rendered
    assert "private.example.test" not in rendered
    assert "also-private" not in rendered
    assert summary["shapes"] == {
        "options": "object:1",
        "query": f"string:{len(secret_query)}",
        "urls": "array:1",
    }
