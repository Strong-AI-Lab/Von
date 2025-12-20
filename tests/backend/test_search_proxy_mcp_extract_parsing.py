import asyncio

from src.backend.integrations.internal_mcp.search_proxy_mcp import (
    SearchMCPProxy,
    SearchProxyConfig,
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
