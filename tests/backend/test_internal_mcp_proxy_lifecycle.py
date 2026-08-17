"""Cross-execution lifecycle tests for internal MCP proxy acquisition."""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.parametrize(
    ("module_name", "builder_name", "proxy_class_name"),
    [
        (
            "src.backend.integrations.internal_mcp.arxiv_proxy_mcp",
            "resolve_arxiv_cache_root",
            "ArxivMCPProxy",
        ),
        (
            "src.backend.integrations.internal_mcp.github_proxy_mcp",
            "_build_github_config",
            "GitHubMCPProxy",
        ),
        (
            "src.backend.integrations.internal_mcp.jira_proxy_mcp",
            "_build_jira_config",
            "JiraMCPProxy",
        ),
        (
            "src.backend.integrations.internal_mcp.linkedin_proxy_mcp",
            "_build_linkedin_config",
            "LinkedInMCPProxy",
        ),
    ],
)
def test_proxy_acquisition_is_safe_across_fresh_event_loops(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    builder_name: str,
    proxy_class_name: str,
) -> None:
    module = importlib.import_module(module_name)
    caller_count = 8
    start_barrier = threading.Barrier(caller_count)

    class _Proxy:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.usable = True
            # Keep construction in flight so callers from independent loops
            # contend on the public getter rather than running sequentially.
            time.sleep(0.05)

    monkeypatch.setattr(module, "_proxy_instance", None)
    monkeypatch.setattr(module, proxy_class_name, _Proxy)
    if module_name.endswith("arxiv_proxy_mcp"):
        monkeypatch.setattr(module, builder_name, lambda: Path("test-arxiv-cache"))
    else:
        config = SimpleNamespace(command="test", args=["server"])
        monkeypatch.setattr(module, builder_name, lambda: config)

    get_proxy = getattr(module, f"get_{module_name.rsplit('.', 1)[-1].split('_', 1)[0]}_proxy")

    def _invoke_from_fresh_loop() -> Any:
        start_barrier.wait(timeout=2.0)
        return asyncio.run(asyncio.wait_for(get_proxy(), timeout=2.0))

    with concurrent.futures.ThreadPoolExecutor(max_workers=caller_count) as executor:
        futures = [executor.submit(_invoke_from_fresh_loop) for _ in range(caller_count)]
        proxies = [future.result(timeout=3.0) for future in futures]

    assert len(proxies) == caller_count
    assert all(isinstance(proxy, _Proxy) and proxy.usable for proxy in proxies)
