import asyncio

import pytest

from src.backend.integrations.internal_mcp.mcp_proxy_base import MCPToolClientError
from src.backend.integrations.internal_mcp.search_proxy_mcp import (
    SearchMCPProxy,
    SearchProxyConfig,
    SearchProxyError,
)


class _StubClient:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool_name, arguments, *, text_parser=None):
        self.calls.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "text_parser_is_none": text_parser is None,
            }
        )
        # Simulate Tavily extract JSON-like response
        if tool_name == "tavily-extract":
            return {
                "results": [
                    {
                        "url": arguments.get("urls", [None])[0],
                        "title": "Example Title",
                        "content": "Example content",
                    }
                ]
            }
        return {"results": []}


def test_extract_does_not_use_search_text_parser_and_normalises_output():
    proxy = SearchMCPProxy(SearchProxyConfig(api_key="dummy"))
    proxy._client = _StubClient()  # type: ignore[attr-defined]

    result = asyncio.run(proxy.extract(url="https://example.com"))

    assert proxy._client.calls[0]["tool_name"] == "tavily-extract"  # type: ignore[attr-defined]
    assert proxy._client.calls[0]["text_parser_is_none"] is True  # type: ignore[attr-defined]
    assert result["success"] is True
    assert result["url"] == "https://example.com"
    assert result["title"] == "Example Title"
    assert "Example content" in result["content"]


def test_search_uses_search_text_parser():
    proxy = SearchMCPProxy(SearchProxyConfig(api_key="dummy"))
    proxy._client = _StubClient()  # type: ignore[attr-defined]

    _ = asyncio.run(proxy.search(query="hello"))

    assert proxy._client.calls[0]["tool_name"] == "tavily-search"  # type: ignore[attr-defined]
    assert proxy._client.calls[0]["text_parser_is_none"] is False  # type: ignore[attr-defined]


def test_search_retries_once_on_transient_taskgroup_error():
    class _RetryThenSuccessClient:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, tool_name, arguments, *, text_parser=None):
            self.calls += 1
            if self.calls == 1:
                raise MCPToolClientError(
                    "unhandled errors in a TaskGroup (1 sub-exception)"
                )
            return {
                "results": [
                    {
                        "title": "Recovered",
                        "url": "https://example.com",
                        "content": "ok",
                    }
                ]
            }

    proxy = SearchMCPProxy(
        SearchProxyConfig(
            api_key="dummy",
            max_transient_retries=1,
            retry_backoff_sec=0.0,
        )
    )
    proxy._client = _RetryThenSuccessClient()  # type: ignore[attr-defined]

    result = asyncio.run(proxy.search(query="hello"))

    assert proxy._client.calls == 2  # type: ignore[attr-defined]
    assert result.get("results")


def test_search_does_not_retry_non_transient_error():
    class _AlwaysBadRequestClient:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, tool_name, arguments, *, text_parser=None):
            self.calls += 1
            raise MCPToolClientError("invalid request payload shape")

    proxy = SearchMCPProxy(
        SearchProxyConfig(
            api_key="dummy",
            max_transient_retries=1,
            retry_backoff_sec=0.0,
        )
    )
    proxy._client = _AlwaysBadRequestClient()  # type: ignore[attr-defined]

    with pytest.raises(SearchProxyError) as exc_info:
        asyncio.run(proxy.search(query="hello"))

    assert proxy._client.calls == 1  # type: ignore[attr-defined]
    details = exc_info.value.details.to_dict()
    assert details.get("error_type") == "mcp_tool_error"
    assert details.get("retry_attempt") == 0
    assert details.get("retry_max") == 1


def test_catalogue_extract_url_works_inside_running_event_loop(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.integrations.internal_mcp import search_proxy_mcp

    class _DummyProxy:
        async def extract(self, *, url):
            return {"success": True, "url": url, "title": "t", "content": "x"}

    async def _fake_get_search_proxy():
        return _DummyProxy()

    monkeypatch.setattr(search_proxy_mcp, "get_search_proxy", _fake_get_search_proxy)

    async def _runner():
        return catalogue._extract_url(url="https://example.com")

    result = asyncio.run(_runner())
    assert result["success"] is True
    assert result["url"] == "https://example.com"
