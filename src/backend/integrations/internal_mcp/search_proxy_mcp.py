"""Search MCP proxy using official MCP SDK client.

Manages subprocess communication with external search MCP servers (primarily Tavily)
using the MCP protocol.
"""

from __future__ import annotations

import asyncio
import re
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError

logger = logging.getLogger(__name__)
_LOG_TAG = "[search_proxy]"


def _parse_tavily_text_response(text: str) -> Dict[str, Any]:
    """Parse Tavily's text-formatted response into structured data.

    Tavily-mcp returns results as formatted text like:
    "Detailed Results:\n\nTitle: ...\nURL: ...\nContent: ...\n\nTitle: ..."

    This parser uses regex to robustly extract structured data from that format.
    """
    # Regex to capture Title, URL, and multi-line Content for each result block
    pattern = re.compile(
        r"Title:\s*(?P<title>.*?)\s*\n"
        r"URL:\s*(?P<url>.*?)\s*\n"
        r"Content:\s*(?P<content>.*?)\s*(?=\n\nTitle:|\Z)",
        re.DOTALL | re.MULTILINE,
    )

    results = [
        {
            "title": match.group("title").strip(),
            "url": match.group("url").strip(),
            "content": match.group("content").strip().replace('\n', ' '),
        }
        for match in pattern.finditer(text)
    ]

    return {'results': results}


@dataclass
class SearchProxyConfig:
    """Configuration for Search MCP subprocess."""
    api_key: str
    command: str = "npx"
    timeout_sec: float = 30.0


class SearchProxyError(Exception):
    """Raised when search proxy operations fail."""


class SearchMCPProxy:
    """Manages external search MCP server (Tavily) subprocess using MCP SDK client."""

    def __init__(self, config: SearchProxyConfig):
        self._config = config
        self._client = self._build_client()

    def _build_client(self) -> MCPStdIOClient:
        env = {"TAVILY_API_KEY": self._config.api_key}
        config = MCPServerConfig(
            command=self._config.command,
            args=["-y", "tavily-mcp@latest"],
            env=env,
            timeout_sec=self._config.timeout_sec,
            log_tag=_LOG_TAG,
        )
        return MCPStdIOClient(config)

    async def _call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call an MCP tool and return the result.

        Creates a new MCP session for each tool call to avoid lifecycle issues.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool result (parsed from response)

        Raises:
            SearchProxyError: If tool call fails
        """
        try:
            return await self._client.call_tool(
                tool_name,
                arguments,
                text_parser=_parse_tavily_text_response,
            )
        except MCPToolClientError as exc:
            raise SearchProxyError(f"Request failed: {exc}") from exc

    async def search(
        self,
        query: str,
        max_results: int = 10,
        search_depth: str = "basic",
        include_domains: Optional[list[str]] = None,
        exclude_domains: Optional[list[str]] = None,
        include_answer: bool = False,
        include_raw_content: bool = False,
        include_images: bool = False,
    ) -> Dict[str, Any]:
        """Perform a web search using Tavily.

        Args:
            query: Search query
            max_results: Maximum number of results (default: 10)
            search_depth: Search depth - "basic" or "advanced" (default: "basic")
            include_domains: Whitelist of domains to search (optional)
            exclude_domains: Blacklist of domains to exclude (optional)
            include_answer: Include AI-generated direct answer (default: False)
            include_raw_content: Include full page content (default: False)
            include_images: Include image URLs in results (default: False)

        Returns:
            Dict with search results containing:
                - results: List of search results (title, url, content snippet)
                - answer: Direct answer if include_answer=True
                - images: Image URLs if include_images=True
        """
        # Collect all arguments into a dictionary, filtering out None/default values
        arguments = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth if search_depth != "basic" else None,
            "include_domains": include_domains,
            "exclude_domains": exclude_domains,
            "include_answer": include_answer or None,
            "include_raw_content": include_raw_content or None,
            "include_images": include_images or None,
        }

        # Filter out keys with None or False values to send a clean payload
        final_arguments = {k: v for k, v in arguments.items() if v}

        return await self._call_tool("tavily-search", final_arguments)

    async def context_search(
        self,
        query: str,
        context: str,
        max_results: int = 10,
        search_depth: str = "basic",
        include_answer: bool = False,
    ) -> Dict[str, Any]:
        """Perform a context-aware search optimised for specific topics.

        Note: The tavily-mcp package does not have a dedicated `context_search` tool.
        This method simulates the behavior by embedding the provided context directly
        into the query string sent to the standard `tavily-search` tool.

        Args:
            query: Search query
            context: Additional context to focus the search
            max_results: Maximum number of results
            search_depth: Search depth - "basic" or "advanced"
            include_answer: Include AI-generated direct answer

        Returns:
            Dict with context-enhanced search results
        """
        arguments: Dict[str, Any] = {
            "query": query,
            "context": context,
            "max_results": max_results,
        }
        if search_depth != "basic":
            arguments["search_depth"] = search_depth
        if include_answer:
            arguments["include_answer"] = include_answer

        # Note: tavily-mcp doesn't have a separate context_search tool
        # Use tavily-search with context embedded in query
        combined_query = f"{query} (context: {context})"
        arguments["query"] = combined_query
        return await self._call_tool("tavily-search", arguments)

    async def qna_search(
        self,
        query: str,
        max_results: int = 5,
        search_depth: str = "advanced",
    ) -> Dict[str, Any]:
        """Perform a question-answering optimised search.

        Note: The tavily-mcp package does not have a dedicated `qna_search` tool.
        This method simulates the behavior by calling the standard `tavily-search`
        tool with the `include_answer=True` parameter, which is designed for
        question-answering scenarios.

        Args:
            query: Question to answer
            max_results: Maximum number of results
            search_depth: Search depth - "basic" or "advanced"

        Returns:
            Dict with answer-focused search results
        """
        arguments: Dict[str, Any] = {
            "query": query,
            "max_results": max_results,
        }
        if search_depth != "advanced":
            arguments["search_depth"] = search_depth

        # Note: tavily-mcp doesn't have a separate qna_search tool
        # Use tavily-search with include_answer=True for Q&A behaviour
        arguments["include_answer"] = True
        return await self._call_tool(
            "tavily-search",
            arguments,
        )

    async def extract(
        self,
        url: str,
    ) -> Dict[str, Any]:
        """Extract content from a specific URL.

        Args:
            url: URL to extract content from

        Returns:
            Dict with extracted content
        """
        return await self._call_tool("tavily-extract", {"urls": [url]})

    def get_stats(self) -> Dict[str, int]:
        """Get proxy statistics.

        Returns:
            Dict with call_count and error_count
        """
        return {
            "call_count": self._client.call_count,
            "error_count": self._client.error_count,
        }


# Singleton instance
_proxy_instance: Optional[SearchMCPProxy] = None
_proxy_lock = asyncio.Lock()


async def get_search_proxy() -> SearchMCPProxy:
    """Get or create the global search proxy instance.

    Raises:
        SearchProxyError: If TAVILY_API_KEY is not set
    """
    global _proxy_instance

    async with _proxy_lock:
        if _proxy_instance is None:
            # Get API key from environment
            api_key = os.environ.get("TAVILY_API_KEY")
            if not api_key:
                raise SearchProxyError(
                    "TAVILY_API_KEY environment variable not set. "
                    "Set with: $env:TAVILY_API_KEY = 'your-key-here'"
                )

            config = SearchProxyConfig(api_key=api_key)
            _proxy_instance = SearchMCPProxy(config)
            logger.info("%s Initialised search proxy with Tavily MCP", _LOG_TAG)

        return _proxy_instance
