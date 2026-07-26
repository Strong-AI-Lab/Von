"""Smoke tests for GitHub MCP integration in the internal MCP catalogue."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.backend.integrations.internal_mcp.catalogue import (
    _github_create_branch,
    _github_get_file_contents,
    _github_issue_read,
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
            assert tool_name == "get_file_contents"
            assert arguments["owner"] == "Strong-AI-Lab"
            assert arguments["repo"] == "Von"
            return {
                "path": arguments.get("path"),
                "content": "example",
            }

        async def list_tools(self):
            return [{"name": "get_file_contents"}]

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
    assert payload.get("tool") == "github_get_file_contents"
    assert payload.get("proxy_tool") == "get_file_contents"
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
    assert payload.get("error_details", {}).get("proxy_tool") == "get_file_contents"


def test_github_pull_request_read_translates_supported_methods(monkeypatch) -> None:
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def call_tool(self, tool_name: str, arguments: dict):
            assert tool_name == "get_pull_request_status"
            assert arguments == {
                "owner": "Strong-AI-Lab",
                "repo": "Von",
                "pullNumber": 42,
            }
            return {"state": "success"}

        async def list_tools(self):
            return [{"name": "get_pull_request_status"}]

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
        "github_pull_request_read",
        {
            "owner": "Strong-AI-Lab",
            "repo": "Von",
            "pullNumber": 42,
            "method": "get_status",
        },
    )
    payload = result.payload

    assert payload.get("success") is True
    assert payload.get("tool") == "github_pull_request_read"
    assert payload.get("proxy_tool") == "get_pull_request_status"
    assert payload.get("state") == "success"


def test_github_issue_read_rejects_unsupported_methods() -> None:
    result = _github_issue_read(
        owner="Strong-AI-Lab",
        repo="Von",
        issue_number=7,
        method="get_comments",
    )
    assert result.get("success") is False
    assert result.get("error_code") == "unsupported_operation"


def test_github_create_pull_request_success_through_gateway_invoke(monkeypatch) -> None:
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def call_tool(self, tool_name: str, arguments: dict):
            assert tool_name == "create_pull_request"
            assert arguments["owner"] == "Strong-AI-Lab"
            assert arguments["repo"] == "Von"
            return {"number": 42, "html_url": "https://example.test/pr/42"}

        async def list_tools(self):
            return [{"name": "create_pull_request"}]

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
    assert payload.get("proxy_tool") == "create_pull_request"


def test_github_build_env_prefers_repo_dotenv_token_without_leaking_process_secrets(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    import src.backend.integrations.internal_mcp.github_proxy_mcp as github_proxy_mcp
    import src.backend.utils.runtime_env as runtime_env

    dotenv_token = "ghp_dotenv_token_for_test"
    (tmp_path / ".env").write_text(
        f"GITHUB_PERSONAL_ACCESS_TOKEN={dotenv_token}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_env, "get_project_root", lambda: tmp_path)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_stale_shell_token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghu_vscode_injected_token")
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-openai-secret")
    monkeypatch.setenv("VON_GITHUB_MCP_ARGS", "sentinel-config-argument")
    monkeypatch.delenv("GITHUB_VON_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    caplog.set_level("INFO")
    env = github_proxy_mcp._build_github_env()

    assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == dotenv_token
    assert env["GITHUB_TOKEN"] == dotenv_token
    assert env["GH_TOKEN"] == dotenv_token
    assert (
        github_proxy_mcp.os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"]
        == "ghp_stale_shell_token"
    )
    assert "OPENAI_API_KEY" not in env
    assert "VON_GITHUB_MCP_ARGS" not in env
    assert "sentinel-openai-secret" not in caplog.text
    assert dotenv_token not in caplog.text


@pytest.mark.asyncio
async def test_github_proxy_initialisation_does_not_log_config_arguments(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    import src.backend.integrations.internal_mcp.github_proxy_mcp as github_proxy_mcp
    import src.backend.utils.runtime_env as runtime_env

    config_secret = "sentinel-private-config-argument"
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "GITHUB_PERSONAL_ACCESS_TOKEN=ghp_test_token",
                f"VON_GITHUB_MCP_ARGS=-y package --header {config_secret}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_env, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(github_proxy_mcp, "_proxy_instance", None)
    monkeypatch.setattr(github_proxy_mcp, "_proxy_lock", asyncio.Lock())
    caplog.set_level("INFO")

    proxy = await github_proxy_mcp.get_github_proxy()

    assert proxy is not None
    assert config_secret not in caplog.text


def test_github_auth_config_uses_repo_dotenv_token_through_gateway_invoke(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.backend.utils.runtime_env as runtime_env
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    dotenv_token = "ghp_gateway_dotenv_token"
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"GITHUB_PERSONAL_ACCESS_TOKEN={dotenv_token}",
                "VON_GITHUB_MCP_ARGS=-y @modelcontextprotocol/server-github",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_env, "get_project_root", lambda: tmp_path)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_stale_shell_token")
    monkeypatch.delenv("GITHUB_VON_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghu_vscode_injected_token")
    monkeypatch.delenv("GH_TOKEN", raising=False)

    class _FakeProxy:
        async def call_tool(self, tool_name: str, arguments: dict):  # noqa: ARG002
            raise AssertionError("github_get_auth_config should not call GitHub tools")

        async def list_tools(self):
            return [{"name": "github_get_file_contents"}]

        def get_stats(self):
            return {"call_count": 0, "error_count": 0}

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
    result = gateway.invoke("github_get_auth_config", {})
    payload = result.payload

    assert payload.get("success") is True
    assert payload.get("token_present") is True
    assert payload.get("token_length") == len(dotenv_token)
    assert payload.get("env_keys_used", {}).get("token") == "GITHUB_PERSONAL_ACCESS_TOKEN"
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in payload.get("dotenv_overrides_applied", [])
    assert payload.get("process_environment_mutated") is False
    assert (
        runtime_env.os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"]
        == "ghp_stale_shell_token"
    )
    assert payload.get("proxy_tools_available") is True
