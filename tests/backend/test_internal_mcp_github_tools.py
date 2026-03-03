"""Smoke tests for GitHub MCP integration in the internal MCP catalogue."""

from __future__ import annotations

from src.backend.integrations.internal_mcp.catalogue import (
    _github_create_branch,
    _github_create_pull_request,
    _github_get_file_contents,
    _github_search_code,
    build_default_catalogue,
)


def test_github_methods_registered_in_catalogue() -> None:
    catalogue = build_default_catalogue()
    names = set(catalogue.list_methods())
    assert "github_get_auth_config" in names
    assert "github_list_tools" in names
    assert "github_get_me" in names
    assert "github_get_file_contents" in names
    assert "github_list_commits" in names
    assert "github_search_code" in names
    assert "github_list_pull_requests" in names
    assert "github_pull_request_read" in names
    assert "github_issue_read" in names
    assert "github_list_releases" in names
    assert "github_get_latest_release" in names
    assert "github_list_tags" in names
    assert "github_list_branches" in names
    assert "github_create_branch" in names
    assert "github_create_or_update_file" in names
    assert "github_create_pull_request" in names
    assert "github_update_pull_request" in names
    assert "github_create_pull_request_with_copilot" in names


def test_github_handlers_require_minimum_fields() -> None:
    err_file = _github_get_file_contents(owner=None, repo=None)
    assert err_file.get("success") is False
    assert "owner" in str(err_file.get("error", "")).lower()

    err_search = _github_search_code(query=None)
    assert err_search.get("success") is False
    assert "query" in str(err_search.get("error", "")).lower()


def test_github_write_tools_allowlist_and_dry_run_defaults() -> None:
    ok_branch = _github_create_branch(
        owner="Strong-AI-Lab",
        repo="Von",
        branch="feature/test",
    )
    assert ok_branch.get("success") is True
    assert ok_branch.get("dry_run") is True
    assert ok_branch.get("executed") is False

    bad_branch = _github_create_branch(
        owner="OtherOrg",
        repo="OtherRepo",
        branch="feature/test",
    )
    assert bad_branch.get("success") is False
    assert bad_branch.get("error_code") == "repository_not_allowlisted"


def test_github_write_tools_require_approval_when_not_dry_run() -> None:
    err = _github_create_branch(
        owner="Strong-AI-Lab",
        repo="Von",
        branch="feature/live",
        dry_run=False,
    )
    assert err.get("success") is False
    assert err.get("error_code") == "approval_required"


def test_github_get_file_contents_success_through_gateway_invoke(monkeypatch) -> None:
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def call_tool(self, tool_name: str, arguments: dict):
            assert tool_name == "github_get_file_contents"
            assert arguments["owner"] == "Strong-AI-Lab"
            assert arguments["repo"] == "Von"
            return {
                "path": arguments.get("path"),
                "content": "example",
            }

        async def list_tools(self):
            return [{"name": "github_get_file_contents"}]

        def get_stats(self):
            return {"call_count": 1, "error_count": 0}

    async def _fake_get_github_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.github_proxy_mcp.get_github_proxy",
        _fake_get_github_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    result = gateway.invoke(
        "github_get_file_contents",
        {"owner": "Strong-AI-Lab", "repo": "Von", "path": "README.md"},
    )
    payload = result.payload
    assert payload.get("success") is True
    assert payload.get("path") == "README.md"


def test_github_proxy_error_through_gateway_invoke(monkeypatch) -> None:
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.github_proxy_mcp import GitHubProxyError
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FailingProxy:
        async def call_tool(self, tool_name: str, arguments: dict):  # noqa: ARG002
            raise GitHubProxyError("proxy unavailable")

        async def list_tools(self):
            return []

        def get_stats(self):
            return {"call_count": 0, "error_count": 1}

    async def _fake_get_github_proxy():
        return _FailingProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.github_proxy_mcp.get_github_proxy",
        _fake_get_github_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    result = gateway.invoke(
        "github_get_file_contents",
        {"owner": "Strong-AI-Lab", "repo": "Von", "path": "README.md"},
    )
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("error_code") == "github_proxy_error"


def test_github_create_pull_request_success_through_gateway_invoke(monkeypatch) -> None:
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def call_tool(self, tool_name: str, arguments: dict):
            assert tool_name == "github_create_pull_request"
            assert arguments["owner"] == "Strong-AI-Lab"
            assert arguments["repo"] == "Von"
            return {"number": 42, "html_url": "https://example.test/pr/42"}

        async def list_tools(self):
            return [{"name": "github_create_pull_request"}]

        def get_stats(self):
            return {"call_count": 1, "error_count": 0}

    async def _fake_get_github_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.github_proxy_mcp.get_github_proxy",
        _fake_get_github_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    result = gateway.invoke(
        "github_create_pull_request",
        {
            "owner": "Strong-AI-Lab",
            "repo": "Von",
            "title": "Test PR",
            "head": "feature/test",
            "base": "main",
            "dry_run": False,
            "approved": True,
        },
    )
    payload = result.payload
    assert payload.get("success") is True
    assert payload.get("executed") is True
    assert payload.get("action") == "create_pull_request"
