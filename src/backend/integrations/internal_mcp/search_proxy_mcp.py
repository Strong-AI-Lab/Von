"""Bounded Tavily search adapter.

Von already depends on Tavily's official Python client, so search operations use
that client directly.  Each operation creates and closes its own async client on
the calling event loop.  This keeps the adapter safe across the sync catalogue,
its running-loop worker bridge, and direct async callers without retaining a
cross-loop HTTP client or launching a Node subprocess.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tavily import AsyncTavilyClient

from .transport import get_internal_mcp_execution_scope

logger = logging.getLogger(__name__)
_LOG_TAG = "[search_proxy]"


@dataclass(frozen=True)
class SearchProxyConfig:
    """Configuration for bounded Tavily API calls."""

    api_key: str
    timeout_sec: float = 30.0


@dataclass(frozen=True)
class SearchProxyErrorDetails:
    """Structured details for a failed Tavily operation."""

    error_type: str
    message: str
    tool_name: str | None = None
    url: str | None = None
    query: str | None = None
    duration_ms: float | None = None
    timestamp_utc: str | None = None
    underlying_error: str | None = None
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "error_type": self.error_type,
            "message": self.message,
        }
        if self.tool_name:
            details["tool_name"] = self.tool_name
        if self.url:
            details["url"] = self.url
        if self.query:
            details["query"] = self.query
        if self.duration_ms is not None:
            details["duration_ms"] = round(self.duration_ms, 2)
        if self.timestamp_utc:
            details["timestamp_utc"] = self.timestamp_utc
        if self.underlying_error:
            details["underlying_error"] = self.underlying_error
        if self.suggestions:
            details["suggestions"] = list(self.suggestions)
        return details


class SearchProxyError(Exception):
    """Raised when a Tavily operation fails."""

    def __init__(
        self,
        message: str,
        details: SearchProxyErrorDetails | None = None,
    ):
        super().__init__(message)
        self.details = details or SearchProxyErrorDetails(
            error_type="unknown",
            message=message,
        )


class SearchMCPProxy:
    """Expose Von's existing search interface through Tavily's Python SDK."""

    def __init__(
        self,
        config: SearchProxyConfig,
        *,
        client_factory: Callable[..., Any] | None = None,
    ):
        self._config = config
        self._client_factory = client_factory or AsyncTavilyClient
        self._telemetry_lock = threading.Lock()
        self._call_count = 0
        self._error_count = 0
        self._total_duration_ms = 0.0
        self._recent_calls: list[dict[str, Any]] = []

    def _operation_timeout_sec(self) -> float:
        """Return the time available without exceeding the caller's scope."""

        configured = max(0.001, float(self._config.timeout_sec))
        scope = get_internal_mcp_execution_scope()
        if scope is None:
            return configured
        if scope.cancellation_requested:
            return 0.0

        available = min(configured, scope.remaining_seconds)
        if available <= 0.01:
            return 0.0

        # Leave a small portion of the enclosing budget for cleanup and for the
        # catalogue to serialise the result.  This is bounded by the caller's
        # actual remaining time, rather than imposing another independent limit.
        reserve = min(1.0, available * 0.1)
        operation_timeout = available - reserve
        return operation_timeout if operation_timeout > 0.001 else 0.0

    @staticmethod
    async def _close_client(client: Any) -> None:
        close = getattr(client, "close", None)
        if not callable(close):
            return
        close_result = close()
        if inspect.isawaitable(close_result):
            await close_result

    def _record_call(
        self,
        *,
        operation: str,
        duration_ms: float,
        success: bool,
        error_type: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "tool_name": operation,
            "duration_ms": round(duration_ms, 2),
            "success": success,
            "timestamp_utc": datetime.now(UTC).isoformat(),
        }
        if error_type:
            item["error_type"] = error_type

        with self._telemetry_lock:
            self._call_count += 1
            if not success:
                self._error_count += 1
            self._total_duration_ms += duration_ms
            self._recent_calls.append(item)
            del self._recent_calls[:-10]

    @staticmethod
    def _collect_exception_text(exc: BaseException) -> str:
        """Retain useful nested SDK/HTTP error text without exposing a traceback."""

        seen: set[int] = set()
        chunks: list[str] = []

        def _walk(node: BaseException) -> None:
            if id(node) in seen:
                return
            seen.add(id(node))
            message = str(node).strip()
            chunks.append(
                f"{type(node).__name__}: {message}" if message else type(node).__name__
            )
            cause = node.__cause__
            if isinstance(cause, BaseException):
                _walk(cause)
            context = node.__context__
            if isinstance(context, BaseException) and context is not cause:
                _walk(context)
            nested = getattr(node, "exceptions", ())
            if isinstance(nested, (list, tuple)):
                for child in nested:
                    if isinstance(child, BaseException):
                        _walk(child)

        _walk(exc)
        return " | ".join(dict.fromkeys(chunks))

    @staticmethod
    def _classify_error(error_text: str) -> tuple[str, list[str]]:
        lowered = error_text.lower()
        if "timeout" in lowered or "timed out" in lowered:
            return (
                "timeout",
                [
                    "The Tavily request exceeded the available time",
                    "Try again or use a narrower request",
                ],
            )
        if (
            "api key" in lowered
            or "unauthoriz" in lowered
            or "forbidden" in lowered
            or "401" in lowered
            or "403" in lowered
        ):
            return (
                "api_key_error",
                [
                    "Check that TAVILY_API_KEY is valid",
                    "Check the Tavily account status",
                ],
            )
        if "rate limit" in lowered or "429" in lowered:
            return (
                "rate_limit",
                ["The Tavily rate limit was reached; try again later"],
            )
        if (
            "connection refused" in lowered
            or "connecterror" in lowered
            or "network" in lowered
        ):
            return (
                "connection_error",
                ["Check network access to the Tavily API"],
            )
        return (
            "search_api_error",
            ["Check the request and try another query or source if appropriate"],
        )

    async def _invoke(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        context_url: str | None = None,
        context_query: str | None = None,
    ) -> Any:
        """Execute one SDK operation once within the active caller deadline."""

        started = time.perf_counter()
        timestamp_utc = datetime.now(UTC).isoformat()
        timeout_sec = self._operation_timeout_sec()
        client: Any | None = None
        result: Any = None
        failure: Exception | None = None

        if timeout_sec <= 0.0:
            failure = TimeoutError("Tavily caller deadline was already exhausted")
        else:
            try:
                client = self._client_factory(api_key=self._config.api_key)
                sdk_operation = getattr(client, operation)
                call_arguments = {**arguments, "timeout": timeout_sec}
                async with asyncio.timeout(timeout_sec):
                    result = await sdk_operation(**call_arguments)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - translated to typed boundary
                failure = exc
            finally:
                if client is not None:
                    try:
                        await self._close_client(client)
                    except Exception:
                        logger.warning(
                            "%s Failed to close Tavily client after %s",
                            _LOG_TAG,
                            operation,
                            exc_info=True,
                        )

        duration_ms = (time.perf_counter() - started) * 1000
        if failure is None:
            self._record_call(
                operation=operation,
                duration_ms=duration_ms,
                success=True,
            )
            return result

        error_text = self._collect_exception_text(failure)
        error_type, suggestions = self._classify_error(error_text)
        self._record_call(
            operation=operation,
            duration_ms=duration_ms,
            success=False,
            error_type=error_type,
        )
        details = SearchProxyErrorDetails(
            error_type=error_type,
            message=f"Tavily {operation} failed",
            tool_name=operation,
            url=context_url,
            query=context_query,
            duration_ms=duration_ms,
            timestamp_utc=timestamp_utc,
            underlying_error=error_text[:500],
            suggestions=suggestions,
        )
        raise SearchProxyError(
            f"Tavily {operation} failed: {failure}",
            details=details,
        ) from failure

    async def search(
        self,
        query: str,
        max_results: int = 10,
        search_depth: str = "basic",
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        include_answer: bool = False,
        include_raw_content: bool = False,
        include_images: bool = False,
    ) -> dict[str, Any]:
        """Search the web and return Tavily's result dictionary unchanged."""

        return await self._invoke(
            "search",
            {
                "query": query,
                "max_results": max_results,
                "search_depth": search_depth,
                "include_domains": include_domains,
                "exclude_domains": exclude_domains,
                "include_answer": include_answer,
                "include_raw_content": include_raw_content,
                "include_images": include_images,
            },
            context_query=query,
        )

    async def context_search(
        self,
        query: str,
        context: str,
        max_results: int = 10,
        search_depth: str = "basic",
        include_answer: bool = False,
    ) -> dict[str, Any]:
        """Search with the supplied background represented in the query."""

        combined_query = f"{query} (context: {context})"
        return await self._invoke(
            "search",
            {
                "query": combined_query,
                "max_results": max_results,
                "search_depth": search_depth,
                "include_answer": include_answer,
            },
            context_query=combined_query,
        )

    async def qna_search(
        self,
        query: str,
        max_results: int = 5,
        search_depth: str = "advanced",
    ) -> dict[str, Any]:
        """Request Tavily's answer alongside ordinary supporting search results."""

        return await self._invoke(
            "search",
            {
                "query": query,
                "max_results": max_results,
                "search_depth": search_depth,
                "include_answer": True,
            },
            context_query=query,
        )

    async def extract(self, url: str) -> dict[str, Any]:
        """Extract one URL and normalise the SDK response for Von callers."""

        raw = await self._invoke(
            "extract",
            {"urls": [url]},
            context_url=url,
        )

        content: str | None = None
        title: str | None = None
        if isinstance(raw, dict):
            results = raw.get("results")
            if isinstance(results, list) and results:
                first = results[0] if isinstance(results[0], dict) else {}
                title = first.get("title") or first.get("page_title")
                content = (
                    first.get("content")
                    or first.get("raw_content")
                    or first.get("text")
                )
            elif not isinstance(results, list):
                title = raw.get("title")
                content = (
                    raw.get("content") or raw.get("raw_content") or raw.get("text")
                )

        if content is not None and not isinstance(content, str):
            content = str(content)
        if title is not None and not isinstance(title, str):
            title = str(title)

        if content:
            stripped = content.strip()
            if stripped.lower() in {"detailed results:", "detailed results"} or (
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

        return {
            "success": False,
            "url": url,
            "title": title,
            "content": None,
            "error": (
                "No extractable content returned for URL "
                "(page may be JavaScript-rendered or restrict automated extraction)."
            ),
        }

    def _stats_unlocked(self) -> dict[str, Any]:
        success_rate = (
            (self._call_count - self._error_count) / self._call_count * 100
            if self._call_count
            else 100.0
        )
        return {
            "call_count": self._call_count,
            "error_count": self._error_count,
            "total_duration_ms": round(self._total_duration_ms, 2),
            "success_rate_percent": round(success_rate, 1),
            "avg_duration_ms": (
                round(self._total_duration_ms / self._call_count, 2)
                if self._call_count
                else 0.0
            ),
        }

    def get_stats(self) -> dict[str, Any]:
        """Return lightweight aggregate operation telemetry."""

        with self._telemetry_lock:
            return self._stats_unlocked()

    def get_diagnostics(self) -> dict[str, Any]:
        """Return aggregate telemetry and the ten most recent operations."""

        with self._telemetry_lock:
            recent_calls = [dict(item) for item in self._recent_calls]
            return {
                "stats": self._stats_unlocked(),
                "last_call": recent_calls[-1] if recent_calls else None,
                "recent_calls": recent_calls,
                "config": {
                    "transport": "tavily_python",
                    "timeout_sec": self._config.timeout_sec,
                    "api_key_set": bool(self._config.api_key),
                },
            }

    async def check_health(self) -> dict[str, Any]:
        """Check Tavily connectivity with one small search."""

        started = time.perf_counter()
        try:
            result = await self.search(
                query="test",
                max_results=1,
                search_depth="basic",
            )
            return {
                "healthy": True,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "has_results": bool(result.get("results")),
                "timestamp_utc": datetime.now(UTC).isoformat(),
            }
        except SearchProxyError as exc:
            return {
                "healthy": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "error": exc.details.to_dict(),
                "timestamp_utc": datetime.now(UTC).isoformat(),
            }


_proxy_instance: SearchMCPProxy | None = None
_proxy_lock = threading.Lock()


def _try_load_tavily_key_from_dotenv() -> str | None:
    """Read TAVILY_API_KEY from the repo-root .env when the process lacks it."""

    try:
        from dotenv import dotenv_values
    except Exception:  # noqa: BLE001 - optional dependency/load fallback
        return None

    try:
        repo_root = Path(__file__).resolve().parents[4]
    except Exception:  # noqa: BLE001 - defensive path resolution fallback
        repo_root = Path.cwd()
    env_path = repo_root / ".env"
    if not env_path.exists():
        return None
    try:
        raw = dotenv_values(env_path).get("TAVILY_API_KEY")
    except Exception:  # noqa: BLE001 - malformed or unreadable local dotenv
        return None
    if not raw:
        return None
    return str(raw).strip() or None


async def get_search_proxy() -> SearchMCPProxy:
    """Return the process-wide adapter, which creates clients per operation."""

    global _proxy_instance

    with _proxy_lock:
        if _proxy_instance is None:
            api_key = os.environ.get("TAVILY_API_KEY")
            if not api_key:
                api_key = _try_load_tavily_key_from_dotenv()
            if not api_key:
                message = (
                    "TAVILY_API_KEY is not set in the process environment "
                    "or repo-root .env"
                )
                raise SearchProxyError(
                    message,
                    details=SearchProxyErrorDetails(
                        error_type="api_key_error",
                        message=message,
                        suggestions=["Set a valid TAVILY_API_KEY and restart Von"],
                    ),
                )
            _proxy_instance = SearchMCPProxy(SearchProxyConfig(api_key=api_key))
            logger.info("%s Initialised direct Tavily search proxy", _LOG_TAG)

        return _proxy_instance
