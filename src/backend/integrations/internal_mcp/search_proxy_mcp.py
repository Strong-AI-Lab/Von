"""Search MCP proxy using official MCP SDK client.

Manages subprocess communication with external search MCP servers (primarily Tavily)
using the MCP protocol.

Telemetry and Diagnostics
-------------------------
This module provides detailed telemetry for debugging extraction/search failures:

- Each call records timing, response shape, and error details
- Use `get_stats()` for aggregate statistics
- Use `get_diagnostics()` for recent call history
- Use `check_health()` to verify Tavily connectivity

Common failure modes:
- "Request failed: Connection refused" - npx/Tavily subprocess failed to start
- "Request failed: Timeout" - Tavily API slow or unresponsive
- Empty extraction - Target site blocks automated requests or is JS-rendered
"""

from __future__ import annotations

import asyncio
import re
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError

logger = logging.getLogger(__name__)
_LOG_TAG = "[search_proxy]"

# Canonical Tavily tool aliases across MCP package versions.
_TAVILY_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "tavily-search": ("tavily-search", "tavily_search"),
    "tavily-extract": ("tavily-extract", "tavily_extract"),
    "tavily-crawl": ("tavily-crawl", "tavily_crawl"),
    "tavily-map": ("tavily-map", "tavily_map"),
    "tavily-research": ("tavily-research", "tavily_research"),
}


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
    # Keep retry bounded to avoid tool-call thrashing while still covering
    # transient transport/server flakiness (e.g., TaskGroup upstream errors).
    max_transient_retries: int = 1
    retry_backoff_sec: float = 0.35


@dataclass
class SearchProxyErrorDetails:
    """Structured error details for search proxy failures."""

    error_type: str
    message: str
    tool_name: Optional[str] = None
    url: Optional[str] = None
    query: Optional[str] = None
    duration_ms: Optional[float] = None
    timestamp_utc: Optional[str] = None
    underlying_error: Optional[str] = None
    suggestions: List[str] = field(default_factory=list)
    retry_attempt: Optional[int] = None
    retry_max: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "error_type": self.error_type,
            "message": self.message,
        }
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.url:
            d["url"] = self.url
        if self.query:
            d["query"] = self.query
        if self.duration_ms is not None:
            d["duration_ms"] = round(self.duration_ms, 2)
        if self.timestamp_utc:
            d["timestamp_utc"] = self.timestamp_utc
        if self.underlying_error:
            d["underlying_error"] = self.underlying_error
        if self.suggestions:
            d["suggestions"] = self.suggestions
        if self.retry_attempt is not None:
            d["retry_attempt"] = self.retry_attempt
        if self.retry_max is not None:
            d["retry_max"] = self.retry_max
        return d


class SearchProxyError(Exception):
    """Raised when search proxy operations fail.

    Attributes:
        details: Structured error information for diagnostics.
    """

    def __init__(
        self,
        message: str,
        details: Optional[SearchProxyErrorDetails] = None,
    ):
        super().__init__(message)
        self.details = details or SearchProxyErrorDetails(
            error_type="unknown",
            message=message,
        )


class SearchMCPProxy:
    """Manages external search MCP server (Tavily) subprocess using MCP SDK client."""

    def __init__(self, config: SearchProxyConfig):
        self._config = config
        self._client = self._build_client()
        self._available_tools_cache: Optional[set[str]] = None

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

    @staticmethod
    def _classify_error(error_str: str) -> tuple[str, list[str]]:
        """Classify proxy errors and provide actionable suggestions."""
        lowered = error_str.lower()
        if "timeout" in lowered:
            return (
                "timeout",
                [
                    "The Tavily API is slow or unresponsive",
                    "Check your network connection",
                    "Try again in a few moments",
                ],
            )
        if "connection refused" in lowered:
            return (
                "connection_refused",
                [
                    "The Tavily MCP subprocess failed to start",
                    "Ensure Node.js and npx are installed",
                    "Check TAVILY_API_KEY is valid",
                ],
            )
        if "api" in lowered and "key" in lowered:
            return (
                "api_key_error",
                [
                    "TAVILY_API_KEY may be invalid or expired",
                    "Check your Tavily account status",
                ],
            )
        if "rate" in lowered or "limit" in lowered:
            return (
                "rate_limit",
                [
                    "Tavily API rate limit reached",
                    "Wait before retrying",
                    "Consider upgrading your Tavily plan",
                ],
            )
        return (
            "mcp_tool_error",
            [
                "Check the URL is accessible",
                "The target site may block automated requests",
                "Try a different URL or use web search as fallback",
            ],
        )

    @staticmethod
    def _is_transient_error(error_type: str, error_str: str) -> bool:
        """Detect failure classes worth one bounded retry."""
        if error_type in {"timeout", "connection_refused", "rate_limit"}:
            return True
        lowered = error_str.lower()
        # TaskGroup and related transport issues are usually transient in practice.
        transient_markers = (
            "taskgroup",
            "temporar",
            "connection reset",
            "server disconnected",
            "broken pipe",
            "econnreset",
            "503",
            "502",
            "504",
        )
        return any(marker in lowered for marker in transient_markers)

    @staticmethod
    def _collect_exception_text(exc: BaseException) -> str:
        """Flatten nested exception/cause/group text for diagnostics.

        MCP failures are often wrapped by AnyIO/TaskGroup layers, so this
        surfaces buried root causes such as unknown tool IDs.
        """

        seen: set[int] = set()
        chunks: list[str] = []

        def _walk(node: BaseException) -> None:
            node_id = id(node)
            if node_id in seen:
                return
            seen.add(node_id)

            text = str(node).strip()
            if text:
                chunks.append(f"{type(node).__name__}: {text}")

            cause = getattr(node, "__cause__", None)
            if isinstance(cause, BaseException):
                _walk(cause)

            context = getattr(node, "__context__", None)
            if isinstance(context, BaseException) and context is not cause:
                _walk(context)

            sub_exceptions = getattr(node, "exceptions", None)
            if isinstance(sub_exceptions, (list, tuple)):
                for sub in sub_exceptions:
                    if isinstance(sub, BaseException):
                        _walk(sub)

        _walk(exc)

        # Keep ordering but deduplicate repeats from cause/context traversal.
        unique_chunks: list[str] = []
        for chunk in chunks:
            if chunk not in unique_chunks:
                unique_chunks.append(chunk)
        return " | ".join(unique_chunks)

    async def _get_available_tool_names(self) -> set[str]:
        """Best-effort discovery of tool names exposed by current Tavily MCP."""
        if self._available_tools_cache is not None:
            return self._available_tools_cache

        names: set[str] = set()
        try:
            tools = await self._client.list_tools()
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                name = tool.get("name")
                if isinstance(name, str) and name:
                    names.add(name)
        except Exception as exc:
            logger.warning("%s Unable to list Tavily tools for name resolution: %s", _LOG_TAG, exc)

        self._available_tools_cache = names
        return names

    async def _resolve_tool_name_candidates(self, tool_name: str) -> list[str]:
        """Return deterministic candidate tool names for current runtime."""
        configured = _TAVILY_TOOL_ALIASES.get(tool_name, (tool_name,))
        candidates: list[str] = []
        for candidate in configured:
            if candidate not in candidates:
                candidates.append(candidate)

        # Generic swap fallback for future tool ids.
        swapped = (
            tool_name.replace("-", "_")
            if "-" in tool_name
            else tool_name.replace("_", "-")
        )
        if swapped not in candidates:
            candidates.append(swapped)

        available = await self._get_available_tool_names()
        if not available:
            return candidates

        preferred = [candidate for candidate in candidates if candidate in available]
        if preferred:
            return preferred
        return candidates

    async def _call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        context_url: Optional[str] = None,
        context_query: Optional[str] = None,
    ) -> Any:
        """Call an MCP tool and return the result.

        Creates a new MCP session for each tool call to avoid lifecycle issues.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments
            context_url: URL being processed (for error context)
            context_query: Search query (for error context)

        Returns:
            Tool result (parsed from response)

        Raises:
            SearchProxyError: If tool call fails (with detailed diagnostics)
        """
        # Only apply Tavily's formatted-text parser to `tavily-search`.
        # `tavily-extract` frequently returns JSON; forcing the search parser can
        # incorrectly yield empty results (e.g., {"results": []}) and mask the
        # underlying content.
        text_parser = (
            _parse_tavily_text_response
            if tool_name in {"tavily-search", "tavily_search"}
            else None
        )
        retry_max = max(0, int(self._config.max_transient_retries))
        tool_candidates = await self._resolve_tool_name_candidates(tool_name)
        last_error: Optional[SearchProxyError] = None

        for candidate_name in tool_candidates:
            retry_attempt = 0

            while True:
                start_time = time.perf_counter()
                timestamp_utc = datetime.now(timezone.utc).isoformat()
                try:
                    return await self._client.call_tool(
                        candidate_name,
                        arguments,
                        text_parser=text_parser,
                    )
                except MCPToolClientError as exc:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    flattened_error = self._collect_exception_text(exc)
                    error_text_for_classification = flattened_error or str(exc)
                    error_type, suggestions = self._classify_error(error_text_for_classification)
                    is_transient = self._is_transient_error(error_type, error_text_for_classification)
                    is_unknown_tool = "unknown tool" in error_text_for_classification.lower()

                    details = SearchProxyErrorDetails(
                        error_type=error_type,
                        message=f"Tavily {candidate_name} failed",
                        tool_name=candidate_name,
                        url=context_url,
                        query=context_query,
                        duration_ms=duration_ms,
                        timestamp_utc=timestamp_utc,
                        underlying_error=error_text_for_classification[:500],
                        suggestions=suggestions,
                        retry_attempt=retry_attempt,
                        retry_max=retry_max,
                    )

                    if is_unknown_tool:
                        logger.warning(
                            "%s %s unknown-tool error after %.1fms; trying alias candidate if available: %s",
                            _LOG_TAG,
                            candidate_name,
                            duration_ms,
                            error_text_for_classification,
                        )
                        last_error = SearchProxyError(
                            f"Request failed: {exc}",
                            details=details,
                        )
                        break

                    if is_transient and retry_attempt < retry_max:
                        retry_attempt += 1
                        backoff_sec = max(0.0, float(self._config.retry_backoff_sec)) * retry_attempt
                        logger.warning(
                            "%s %s transient failure (attempt %d/%d) after %.1fms: [%s] %s; retrying in %.2fs",
                            _LOG_TAG,
                            candidate_name,
                            retry_attempt,
                            retry_max,
                            duration_ms,
                            error_type,
                            error_text_for_classification,
                            backoff_sec,
                        )
                        if backoff_sec > 0:
                            await asyncio.sleep(backoff_sec)
                        continue

                    logger.error(
                        "%s %s failed after %.1fms: [%s] %s",
                        _LOG_TAG,
                        candidate_name,
                        duration_ms,
                        error_type,
                        error_text_for_classification,
                    )

                    raise SearchProxyError(
                        f"Request failed: {exc}",
                        details=details,
                    ) from exc

            # Candidate loop continues for unknown-tool fallback only.
            continue

        if last_error is not None:
            raise last_error

        raise SearchProxyError(
            f"Request failed: unable to resolve callable tool for {tool_name}",
            details=SearchProxyErrorDetails(
                error_type="tool_resolution_error",
                message=f"No callable Tavily tool candidate succeeded for {tool_name}",
                tool_name=tool_name,
                url=context_url,
                query=context_query,
                suggestions=["Check Tavily MCP tool availability and naming variants"],
            ),
        )

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

        return await self._call_tool(
            "tavily-search",
            final_arguments,
            context_query=query,
        )

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
        return await self._call_tool(
            "tavily-search",
            arguments,
            context_query=combined_query,
        )

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
            context_query=query,
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
        raw = await self._call_tool(
            "tavily-extract",
            {"urls": [url]},
            context_url=url,
        )

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

    def get_stats(self) -> Dict[str, Any]:
        """Get proxy statistics.

        Returns:
            Dict with call_count, error_count, total_duration_ms, and success_rate
        """
        call_count = self._client.call_count
        error_count = self._client.error_count
        success_rate = (
            ((call_count - error_count) / call_count * 100) if call_count > 0 else 100.0
        )
        return {
            "call_count": call_count,
            "error_count": error_count,
            "total_duration_ms": round(self._client.total_duration_ms, 2),
            "success_rate_percent": round(success_rate, 1),
            "avg_duration_ms": (
                round(self._client.total_duration_ms / call_count, 2)
                if call_count > 0
                else 0.0
            ),
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        """Get detailed diagnostics for debugging.

        Returns:
            Dict with stats, last call info, and recent call history
        """
        last_call = None
        if self._client.last_call_telemetry:
            last_call = self._client.last_call_telemetry.to_dict()

        recent_calls = [t.to_dict() for t in self._client.telemetry_history[-10:]]

        return {
            "stats": self.get_stats(),
            "last_call": last_call,
            "recent_calls": recent_calls,
            "config": {
                "command": self._config.command,
                "timeout_sec": self._config.timeout_sec,
                "api_key_set": bool(self._config.api_key),
            },
        }

    async def check_health(self) -> Dict[str, Any]:
        """Check Tavily connectivity with a simple search.

        Returns:
            Dict with healthy (bool), latency_ms, and any error details
        """
        start_time = time.perf_counter()
        try:
            result = await self.search(
                query="test",
                max_results=1,
                search_depth="basic",
            )
            duration_ms = (time.perf_counter() - start_time) * 1000
            has_results = bool(result.get("results"))
            return {
                "healthy": True,
                "latency_ms": round(duration_ms, 2),
                "has_results": has_results,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            }
        except SearchProxyError as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            return {
                "healthy": False,
                "latency_ms": round(duration_ms, 2),
                "error": exc.details.to_dict() if exc.details else str(exc),
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
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
