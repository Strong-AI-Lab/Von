import asyncio
from typing import Any, cast

from mcp.types import Tool
import pytest

from src.backend.integrations.internal_mcp.tool_contract_registry import (
    SURFACE_VONTOLOGY_STDIO,
)
from src.backend.mcp_server import mcp_stdio_server


def _run_list_tools() -> list[Any]:
    list_tools_fn = cast(Any, mcp_stdio_server.list_tools)
    return asyncio.run(list_tools_fn())


def _reset_tool_cache() -> None:
    mcp_stdio_server._TOOL_LIST_CACHE = None


@pytest.fixture(autouse=True)
def _isolate_tool_cache():
    _reset_tool_cache()
    yield
    _reset_tool_cache()


def test_list_tools_prefers_manifest_fallback_before_registry(monkeypatch):
    monkeypatch.setattr(mcp_stdio_server, "_load_tool_list_cache", lambda: None)
    monkeypatch.setattr(
        mcp_stdio_server,
        "_load_tool_list_manifest",
        lambda: [
            Tool(
                name="manifest_tool",
                description="from manifest fallback",
                inputSchema={"type": "object", "properties": {}, "required": []},
            )
        ],
    )

    def _unexpected_registry_build(_surface: str):
        raise AssertionError("Canonical registry build should be skipped")

    persisted: list[list[Tool]] = []
    monkeypatch.setattr(mcp_stdio_server, "get_surface_tool_payloads", _unexpected_registry_build)
    monkeypatch.setattr(
        mcp_stdio_server,
        "_persist_tool_list_cache",
        lambda tools: persisted.append(list(tools)),
    )

    tools = _run_list_tools()

    assert [tool.name for tool in tools] == ["manifest_tool"]
    assert persisted
    assert [tool.name for tool in persisted[0]] == ["manifest_tool"]


def test_list_tools_builds_registry_when_manifest_unavailable(monkeypatch):
    monkeypatch.setattr(mcp_stdio_server, "_load_tool_list_cache", lambda: None)
    monkeypatch.setattr(mcp_stdio_server, "_load_tool_list_manifest", lambda: None)
    monkeypatch.setattr(mcp_stdio_server, "_persist_tool_list_cache", lambda _tools: None)
    monkeypatch.setattr(
        mcp_stdio_server,
        "get_surface_tool_payloads",
        lambda surface: (
            [
                {
                    "name": "registry_tool",
                    "description": "from canonical registry",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                }
            ]
            if surface == SURFACE_VONTOLOGY_STDIO
            else []
        ),
    )

    tools = _run_list_tools()

    assert [tool.name for tool in tools] == ["registry_tool"]
