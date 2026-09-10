import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import types

from src.backend.integrations.internal_mcp import mcp_proxy_base as module


def client_fixture(monkeypatch, *, timeout=1):
    state = {"opened": [], "closed": [], "calls": []}

    @asynccontextmanager
    async def stdio(_params):
        yield object(), object()

    class Session:
        def __init__(self, *_args):
            self.number = len(state["opened"])
            state["opened"].append(self.number)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            state["closed"].append(self.number)
            if state.get("drop_cleanup") and self.number == 0:
                raise RuntimeError("Transport closed")

        async def initialize(self):
            pass

        async def call_tool(self, name, arguments):
            state["calls"].append(self.number)
            if name == "drop" and self.number == 0:
                raise RuntimeError("Transport closed")
            if name == "slow":
                await asyncio.sleep(1)
            return SimpleNamespace(
                content=[types.TextContent(type="text", text='{"ok":true}')]
            )

    monkeypatch.setattr(module, "stdio_client", stdio)
    monkeypatch.setattr(module, "ClientSession", Session)
    client = module.MCPStdIOClient(
        module.MCPServerConfig(
            command="python",
            args=["source.py"],
            timeout_sec=timeout,
            transport_retry_attempts=1,
            transport_retry_backoff_sec=0,
        )
    )
    return client, state


def test_source_scope_reuses_concurrent_reads_and_closes_before_next_scope(monkeypatch):
    client, state = client_fixture(monkeypatch)

    async def run():
        async with client.reuse_session():
            results = await asyncio.gather(
                *(client.call_tool("read", {}) for _ in range(4))
            )
            assert all(result == {"ok": True} for result in results)
            assert state["opened"] == [0]
            assert not state["closed"]
        assert state["closed"] == [0]
        await client.call_tool("read", {})

    asyncio.run(run())
    assert state["opened"] == state["closed"] == [0, 1]
    assert client.call_count == 5
    assert client._capture_session.get() is None


def test_dropped_borrowed_transport_recovers_on_fresh_helpers(monkeypatch):
    client, state = client_fixture(monkeypatch)
    state["drop_cleanup"] = True

    async def run():
        async with client.reuse_session():
            assert await client.call_tool("drop", {}) == {"ok": True}
            assert await client.call_tool("read", {}) == {"ok": True}

    asyncio.run(run())
    assert state["calls"] == [0, 1, 2]
    assert sorted(state["closed"]) == state["opened"]
    assert client.call_count == 2
    assert client._capture_session.get() is None


def test_each_borrowed_call_keeps_its_timeout_without_a_whole_capture_timeout(
    monkeypatch,
):
    client, state = client_fixture(monkeypatch, timeout=0.08)

    async def run():
        async with client.reuse_session():
            await asyncio.sleep(0.12)
            assert await client.call_tool("read", {}) == {"ok": True}
            with pytest.raises(module.MCPToolClientError, match="deadline"):
                await client.call_tool("slow", {})
            assert await client.call_tool("read", {}) == {"ok": True}

    asyncio.run(run())
    assert state["opened"] == [0, 1]
    assert sorted(state["closed"]) == state["opened"]
    assert client.error_count == 1


def test_caller_failure_is_preserved_and_session_context_is_cleared(monkeypatch):
    client, state = client_fixture(monkeypatch)

    async def run():
        with pytest.raises(ValueError, match="caller failure"):
            async with client.reuse_session():
                await client.call_tool("read", {})
                raise ValueError("caller failure")
        assert client._capture_session.get() is None
        assert await client.call_tool("read", {}) == {"ok": True}

    asyncio.run(run())
    assert state["opened"] == state["closed"] == [0, 1]
