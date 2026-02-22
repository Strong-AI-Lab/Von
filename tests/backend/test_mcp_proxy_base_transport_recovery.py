from __future__ import annotations

import asyncio
import types

import pytest
from mcp import types as mcp_types

import src.backend.integrations.internal_mcp.mcp_proxy_base as mcp_proxy_base
from src.backend.integrations.internal_mcp.mcp_proxy_base import (
    MCPServerConfig,
    MCPStdIOClient,
    MCPToolClientError,
)


class _DummyStdioContext:
    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


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
                content=[
                    mcp_types.TextContent(type="text", text='{"success": true}')
                ]
            )

    monkeypatch.setattr(mcp_proxy_base, "stdio_client", lambda _params: _DummyStdioContext())
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

    monkeypatch.setattr(mcp_proxy_base, "stdio_client", lambda _params: _DummyStdioContext())
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

    monkeypatch.setattr(mcp_proxy_base, "stdio_client", lambda _params: _DummyStdioContext())
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
