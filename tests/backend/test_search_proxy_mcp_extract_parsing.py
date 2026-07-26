import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from src.backend.integrations.internal_mcp import search_proxy_mcp
from src.backend.integrations.internal_mcp.search_proxy_mcp import (
    SearchMCPProxy,
    SearchProxyConfig,
    SearchProxyError,
)


@dataclass
class _SDKScenario:
    search_result: dict[str, Any] = field(default_factory=lambda: {"results": []})
    extract_result: dict[str, Any] = field(default_factory=lambda: {"results": []})
    error: Exception | None = None
    delay_sec: float = 0.0
    context_probe: Callable[[], object] | None = None
    observed_context: list[object] = field(default_factory=list)
    clients: list["_FakeTavilyClient"] = field(default_factory=list)

    def factory(self, *, api_key: str) -> "_FakeTavilyClient":
        client = _FakeTavilyClient(self, api_key=api_key)
        self.clients.append(client)
        return client


class _FakeTavilyClient:
    def __init__(self, scenario: _SDKScenario, *, api_key: str):
        self.scenario = scenario
        self.api_key = api_key
        self.created_loop_id = id(asyncio.get_running_loop())
        self.operation_loop_id: int | None = None
        self.close_loop_id: int | None = None
        self.search_calls: list[dict[str, Any]] = []
        self.extract_calls: list[dict[str, Any]] = []
        self.closed = False

    async def _respond(self, result: dict[str, Any]) -> dict[str, Any]:
        self.operation_loop_id = id(asyncio.get_running_loop())
        if self.scenario.context_probe is not None:
            self.scenario.observed_context.append(self.scenario.context_probe())
        if self.scenario.delay_sec:
            await asyncio.sleep(self.scenario.delay_sec)
        if self.scenario.error is not None:
            raise self.scenario.error
        return result

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append(dict(kwargs))
        return await self._respond(self.scenario.search_result)

    async def extract(self, **kwargs: Any) -> dict[str, Any]:
        self.extract_calls.append(dict(kwargs))
        return await self._respond(self.scenario.extract_result)

    async def close(self) -> None:
        self.closed = True
        self.close_loop_id = id(asyncio.get_running_loop())


def _proxy(
    scenario: _SDKScenario,
    *,
    timeout_sec: float = 7.5,
) -> SearchMCPProxy:
    return SearchMCPProxy(
        SearchProxyConfig(api_key="test-key", timeout_sec=timeout_sec),
        client_factory=scenario.factory,
    )


def test_search_forwards_sdk_arguments_and_closes_on_the_calling_loop():
    expected = {
        "results": [
            {
                "title": "Relevant result",
                "url": "https://example.edu/result",
                "content": "result",
            }
        ],
        "answer": "A short answer",
        "images": ["https://example.com/image.png"],
    }
    scenario = _SDKScenario(search_result=expected)
    proxy = _proxy(scenario)

    result = asyncio.run(
        proxy.search(
            query="represented reasoning",
            max_results=4,
            search_depth="advanced",
            include_domains=["example.edu"],
            exclude_domains=["example.net"],
            include_answer=True,
            include_raw_content=True,
            include_images=True,
        )
    )

    assert result is expected
    assert len(scenario.clients) == 1
    client = scenario.clients[0]
    assert client.api_key == "test-key"
    assert client.search_calls == [
        {
            "query": "represented reasoning",
            "max_results": 4,
            "search_depth": "advanced",
            "include_domains": ["example.edu"],
            "exclude_domains": ["example.net"],
            "include_answer": True,
            "include_raw_content": True,
            "include_images": True,
            "timeout": pytest.approx(7.5),
        }
    ]
    assert client.created_loop_id == client.operation_loop_id == client.close_loop_id
    assert client.closed is True
    assert proxy.get_stats()["call_count"] == 1
    assert proxy.get_stats()["error_count"] == 0


def test_extract_uses_sdk_shape_and_normalises_raw_content():
    url = "https://example.edu/record"
    scenario = _SDKScenario(
        extract_result={
            "results": [
                {
                    "url": url,
                    "title": "Example title",
                    "raw_content": "Example content",
                }
            ],
            "failed_results": [],
        }
    )
    proxy = _proxy(scenario, timeout_sec=3.0)

    result = asyncio.run(proxy.extract(url))

    assert result == {
        "success": True,
        "url": url,
        "title": "Example title",
        "content": "Example content",
    }
    client = scenario.clients[0]
    assert client.extract_calls == [{"urls": [url], "timeout": pytest.approx(3.0)}]
    assert client.closed is True
    assert client.created_loop_id == client.operation_loop_id == client.close_loop_id


def test_one_proxy_creates_and_closes_a_client_for_each_event_loop_call():
    scenario = _SDKScenario()
    proxy = _proxy(scenario)

    asyncio.run(proxy.search(query="first"))
    asyncio.run(proxy.search(query="second"))

    assert len(scenario.clients) == 2
    for client in scenario.clients:
        assert client.closed is True
        assert client.created_loop_id == client.operation_loop_id
        assert client.operation_loop_id == client.close_loop_id


def test_extract_reports_an_empty_sdk_result_as_a_non_success():
    scenario = _SDKScenario(
        extract_result={
            "results": [
                {
                    "url": "https://example.com",
                    "title": "Blocked",
                    "raw_content": "Detailed Results:",
                }
            ]
        }
    )

    result = asyncio.run(_proxy(scenario).extract("https://example.com"))

    assert result["success"] is False
    assert result["content"] is None
    assert "No extractable content" in result["error"]


def test_context_search_combines_context_without_an_sdk_context_argument():
    expected = {"results": [{"title": "Result"}], "answer": "Relevant"}
    scenario = _SDKScenario(search_result=expected)
    proxy = _proxy(scenario)

    result = asyncio.run(
        proxy.context_search(
            query="What changed?",
            context="bounded agent architecture",
            max_results=6,
            search_depth="basic",
            include_answer=True,
        )
    )

    assert result is expected
    arguments = scenario.clients[0].search_calls[0]
    assert arguments["query"] == ("What changed? (context: bounded agent architecture)")
    assert arguments["max_results"] == 6
    assert arguments["search_depth"] == "basic"
    assert arguments["include_answer"] is True
    assert "context" not in arguments


def test_qna_search_uses_search_with_answer_and_requested_depth():
    expected = {
        "answer": "Because the evidence supports it.",
        "results": [{"url": "https://example.com/evidence"}],
    }
    scenario = _SDKScenario(search_result=expected)

    result = asyncio.run(
        _proxy(scenario).qna_search(
            query="Why?",
            max_results=3,
            search_depth="advanced",
        )
    )

    assert result is expected
    client = scenario.clients[0]
    assert client.search_calls == [
        {
            "query": "Why?",
            "max_results": 3,
            "search_depth": "advanced",
            "include_answer": True,
            "timeout": pytest.approx(7.5),
        }
    ]
    assert client.extract_calls == []


def test_operation_timeout_is_typed_and_still_closes_the_client():
    scenario = _SDKScenario(delay_sec=0.05)
    proxy = _proxy(scenario, timeout_sec=0.01)

    with pytest.raises(SearchProxyError) as exc_info:
        asyncio.run(proxy.search(query="slow request"))

    assert exc_info.value.details.error_type == "timeout"
    assert exc_info.value.details.query == "slow request"
    assert len(scenario.clients) == 1
    assert scenario.clients[0].closed is True
    stats = proxy.get_stats()
    assert stats["call_count"] == 1
    assert stats["error_count"] == 1
    assert proxy.get_diagnostics()["last_call"]["error_type"] == "timeout"


def test_active_caller_scope_reduces_the_sdk_deadline(monkeypatch):
    scope = SimpleNamespace(cancellation_requested=False, remaining_seconds=0.5)
    monkeypatch.setattr(
        search_proxy_mcp,
        "get_internal_mcp_execution_scope",
        lambda: scope,
    )
    scenario = _SDKScenario()

    asyncio.run(_proxy(scenario, timeout_sec=20.0).search(query="bounded"))

    sdk_timeout = scenario.clients[0].search_calls[0]["timeout"]
    assert sdk_timeout == pytest.approx(0.45)
    assert sdk_timeout < scope.remaining_seconds


def test_cancelled_caller_scope_fails_before_constructing_an_sdk_client(
    monkeypatch,
):
    scope = SimpleNamespace(cancellation_requested=True, remaining_seconds=10.0)
    monkeypatch.setattr(
        search_proxy_mcp,
        "get_internal_mcp_execution_scope",
        lambda: scope,
    )
    scenario = _SDKScenario()

    with pytest.raises(SearchProxyError) as exc_info:
        asyncio.run(_proxy(scenario).search(query="cancelled"))

    assert exc_info.value.details.error_type == "timeout"
    assert scenario.clients == []


def test_sdk_failure_is_typed_closed_and_not_retried():
    scenario = _SDKScenario(error=RuntimeError("HTTP 401 invalid API key"))
    proxy = _proxy(scenario)

    with pytest.raises(SearchProxyError) as exc_info:
        asyncio.run(proxy.search(query="hello"))

    assert exc_info.value.details.error_type == "api_key_error"
    assert "RuntimeError" in (exc_info.value.details.underlying_error or "")
    assert len(scenario.clients) == 1
    assert len(scenario.clients[0].search_calls) == 1
    assert scenario.clients[0].closed is True
    assert proxy.get_stats()["error_count"] == 1


def test_catalogue_search_works_without_a_running_event_loop(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    expected = {"results": [{"title": "Direct catalogue result"}]}
    scenario = _SDKScenario(search_result=expected)
    proxy = _proxy(scenario)

    async def _fake_get_search_proxy():
        return proxy

    monkeypatch.setattr(
        search_proxy_mcp,
        "get_search_proxy",
        _fake_get_search_proxy,
    )

    result = catalogue._search_web(query="catalogue", max_results=1)

    assert result is expected
    assert scenario.clients[0].closed is True


def test_catalogue_running_loop_carries_context_into_worker_sdk_call(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    caller_marker: ContextVar[str] = ContextVar(
        "search_proxy_test_caller_marker",
        default="missing",
    )
    scenario = _SDKScenario(context_probe=caller_marker.get)
    proxy = _proxy(scenario)

    async def _fake_get_search_proxy():
        return proxy

    monkeypatch.setattr(
        search_proxy_mcp,
        "get_search_proxy",
        _fake_get_search_proxy,
    )

    async def _runner():
        token = caller_marker.set("visible-in-worker-sdk")
        try:
            return catalogue._search_web(query="context propagation")
        finally:
            caller_marker.reset(token)

    result = asyncio.run(_runner())

    assert result == {"results": []}
    assert scenario.observed_context == ["visible-in-worker-sdk"]
    client = scenario.clients[0]
    assert client.created_loop_id == client.operation_loop_id == client.close_loop_id
    assert client.closed is True
