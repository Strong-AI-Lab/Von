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
from pathlib import Path
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
            "content": match.group("content").strip().replace("\n", " "),
        }
        for match in pattern.finditer(text)
    ]

    return {"results": results}


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
            # Only apply Tavily's formatted-text parser to `tavily-search`.
            # `tavily-extract` frequently returns JSON; forcing the search parser can
            # incorrectly yield empty results (e.g., {"results": []}) and mask the
            # underlying content.
            text_parser = (
                _parse_tavily_text_response if tool_name == "tavily-search" else None
            )
            return await self._client.call_tool(
                tool_name,
                arguments,
                text_parser=text_parser,
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
        raw = await self._call_tool("tavily-extract", {"urls": [url]})

        # Normalise common Tavily extract shapes into Von's expected output.
        # We keep this tolerant because different MCP versions may return slightly
        # different JSON structures.
        content: str | None = None
        title: str | None = None

        if isinstance(raw, dict):
            if "results" in raw and isinstance(raw.get("results"), list):
                results = raw.get("results")
                if results:
                    first = results[0] if isinstance(results[0], dict) else {}
                    if isinstance(first, dict):
                        title = first.get("title") or first.get("page_title")
                        content = (
                            first.get("content")
                            or first.get("raw_content")
                            or first.get("text")
                        )
            else:
                title = raw.get("title") if isinstance(raw.get("title"), str) else None
                content = (
                    raw.get("content") or raw.get("raw_content") or raw.get("text")
                )

        if content is not None and not isinstance(content, str):
            content = str(content)

        if title is not None and not isinstance(title, str):
            title = str(title)

        if content:
            stripped = content.strip()
            # Tavily sometimes returns a header like "Detailed Results:" with no body.
            # Treat that as an empty extraction.
            if stripped.lower() in {"detailed results:", "detailed results"}:
                content = None
            elif (
                stripped.lower().startswith("detailed results:") and len(stripped) <= 40
            ):
                content = None

        if content:
            return {
                "success": True,
                "url": url,
                "title": title,
                "content": content,
            }

        # Treat empty extraction as a failure so callers don't mistake it for success.
        return {
            "success": False,
            "url": url,
            "title": title,
            "content": None,
            "error": "No extractable content returned for URL (page may be JavaScript-rendered or restrict automated extraction).",
        }

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

            def _try_load_tavily_key_from_dotenv() -> str | None:
                """Attempt to load TAVILY_API_KEY from repo-root .env.

                This is a pragmatic fallback for Windows workflows where `.env` is the source
                of truth but the current process environment may not have been initialised
                from it (process-boundary issues, restarts, alternate entrypoints).
                """

                try:
                    from dotenv import dotenv_values  # type: ignore
                except Exception:
                    return None

                try:
                    repo_root = Path(__file__).resolve().parents[4]
                except Exception:
                    repo_root = Path.cwd()

                env_path = repo_root / ".env"
                if not env_path.exists():
                    return None

                try:
                    values = dotenv_values(env_path)
                except Exception:
                    return None

                raw = values.get("TAVILY_API_KEY")
                if not raw:
                    return None
                return str(raw).strip() or None

            # Get API key from environment, with .env fallback.
            api_key = os.environ.get("TAVILY_API_KEY")
            if not api_key:
                api_key = _try_load_tavily_key_from_dotenv()
                if api_key:
                    os.environ["TAVILY_API_KEY"] = api_key

            if not api_key:
                raise SearchProxyError(
                    "TAVILY_API_KEY environment variable not set. "
                    "If you have it in your repo-root .env, restart the server (or ensure the process loads .env). "
                    "Set in PowerShell with: $env:TAVILY_API_KEY = 'your-key-here'"
                )

            config = SearchProxyConfig(api_key=api_key)
            _proxy_instance = SearchMCPProxy(config)
            logger.info("%s Initialised search proxy with Tavily MCP", _LOG_TAG)

        return _proxy_instance
