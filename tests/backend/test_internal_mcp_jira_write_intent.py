"""JVNAUTOSCI-2137 — Jira MCP write-intent UX tests.

Verify that the simplified write invocation contract holds:

- ``approved=true`` alone executes a write (without requiring the caller to
  also pass ``dry_run=false``).
- ``confirm=true`` is accepted as an alias for ``approved``.
- A bare call with no confirmation flags still defaults to a dry-run preview.
- An explicit ``dry_run=true`` is respected even when ``approved=true``.
- The project allow-list still gates writes regardless of confirmation.
"""

from src.backend.integrations.internal_mcp.catalogue import (
    _jira_create_issue,
    _jira_link_issue,
    _jira_resolve_write_intent,
    _jira_update_issue,
)


def _team_managed_project_issue_type_context(project_key: str) -> dict:
    return {
        "success": True,
        "project_key": project_key,
        "project_id": "10066",
        "project_name": "Zhan von Neumarkt - Automated Science",
        "project_style": "next-gen",
        "project_type_key": "software",
        "is_team_managed": True,
        "issue_type_scheme_supported": False,
        "available_issue_type_names": ["Task", "Epic", "Subtask"],
        "creatable_issue_types": [
            {"id": "10122", "name": "Task", "subtask": False},
        ],
        "project_issue_types": [
            {"id": "10122", "name": "Task", "subtask": False},
        ],
    }


class _FakeProxy:
    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.link_calls: list[dict] = []

    async def get_project_issue_types(self, *, project_key: str):
        return _team_managed_project_issue_type_context(project_key)

    async def create_issue(self, *, payload: dict):
        self.create_calls.append(payload)
        return {"key": "JVNAUTOSCI-9999", "id": "29999"}

    async def update_issue(self, *, issue_key: str, payload: dict):
        self.update_calls.append({"issue_key": issue_key, "payload": payload})
        return {"key": issue_key}

    async def link_issue(self, *, payload: dict):
        self.link_calls.append(payload)
        return {"status_code": 201}


def _install_fake_proxy(monkeypatch) -> _FakeProxy:
    proxy = _FakeProxy()

    async def _fake_get_jira_proxy():
        return proxy

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )
    return proxy


def test_resolve_write_intent_default_is_dry_run() -> None:
    dry_run, approved, execute = _jira_resolve_write_intent({})
    assert dry_run is True
    assert approved is False
    assert execute is False


def test_resolve_write_intent_approved_flips_dry_run_to_false() -> None:
    dry_run, approved, execute = _jira_resolve_write_intent({"approved": True})
    assert dry_run is False
    assert approved is True
    assert execute is False


def test_resolve_write_intent_confirm_is_alias_for_approved() -> None:
    dry_run, approved, execute = _jira_resolve_write_intent({"confirm": True})
    assert dry_run is False
    assert approved is True
    assert execute is False


def test_resolve_write_intent_explicit_dry_run_true_is_respected() -> None:
    dry_run, approved, _ = _jira_resolve_write_intent(
        {"approved": True, "dry_run": True}
    )
    assert dry_run is True
    assert approved is True


def test_resolve_write_intent_execute_without_env_flag_does_not_flip(monkeypatch) -> None:
    monkeypatch.delenv("VON_INTERNAL_MCP_JIRA_EXECUTE_MODE", raising=False)
    dry_run, _, execute = _jira_resolve_write_intent({"execute": True})
    assert execute is True
    # Without the env flag, execute alone is not authority to bypass dry-run.
    assert dry_run is True


def test_resolve_write_intent_execute_with_env_flag_flips(monkeypatch) -> None:
    monkeypatch.setenv("VON_INTERNAL_MCP_JIRA_EXECUTE_MODE", "1")
    dry_run, _, execute = _jira_resolve_write_intent({"execute": True})
    assert execute is True
    assert dry_run is False


def test_jira_create_issue_executes_with_approved_only(monkeypatch) -> None:
    proxy = _install_fake_proxy(monkeypatch)
    result = _jira_create_issue(
        project_key="JVNAUTOSCI",
        issue_type="Task",
        summary="Approved without explicit dry_run=false",
        approved=True,
    )
    assert result.get("success") is True
    assert result.get("dry_run") is False
    assert result.get("executed") is True
    assert len(proxy.create_calls) == 1


def test_jira_create_issue_executes_with_confirm_alias(monkeypatch) -> None:
    proxy = _install_fake_proxy(monkeypatch)
    result = _jira_create_issue(
        project_key="JVNAUTOSCI",
        issue_type="Task",
        summary="Confirm alias path",
        confirm=True,
    )
    assert result.get("success") is True
    assert result.get("dry_run") is False
    assert result.get("executed") is True
    assert len(proxy.create_calls) == 1


def test_jira_create_issue_bare_call_still_previews(monkeypatch) -> None:
    proxy = _install_fake_proxy(monkeypatch)
    result = _jira_create_issue(
        project_key="JVNAUTOSCI",
        issue_type="Task",
        summary="No confirmation",
    )
    assert result.get("success") is True
    assert result.get("dry_run") is True
    assert result.get("executed") is False
    assert proxy.create_calls == []


def test_jira_create_issue_explicit_dry_run_overrides_approved(monkeypatch) -> None:
    proxy = _install_fake_proxy(monkeypatch)
    result = _jira_create_issue(
        project_key="JVNAUTOSCI",
        issue_type="Task",
        summary="Explicit preview",
        approved=True,
        dry_run=True,
    )
    assert result.get("success") is True
    assert result.get("dry_run") is True
    assert result.get("executed") is False
    assert proxy.create_calls == []


def test_jira_create_issue_allowlist_blocks_even_with_confirm(monkeypatch) -> None:
    _install_fake_proxy(monkeypatch)
    result = _jira_create_issue(
        project_key="OTHER",
        issue_type="Task",
        summary="Should be blocked",
        confirm=True,
    )
    assert result.get("success") is False
    assert result.get("error_code") == "project_not_allowlisted"


def test_jira_update_issue_executes_with_approved_only(monkeypatch) -> None:
    proxy = _install_fake_proxy(monkeypatch)
    result = _jira_update_issue(
        issue_key="JVNAUTOSCI-1",
        update_fields={"summary": "Updated via approved-only"},
        approved=True,
    )
    assert result.get("success") is True
    assert result.get("dry_run") is False
    assert result.get("executed") is True
    assert len(proxy.update_calls) == 1


def test_jira_link_issue_executes_with_confirm_alias(monkeypatch) -> None:
    proxy = _install_fake_proxy(monkeypatch)
    result = _jira_link_issue(
        link_type="Relates",
        source_issue_key="JVNAUTOSCI-1",
        target_issue_key="JVNAUTOSCI-2",
        confirm=True,
    )
    assert result.get("success") is True
    assert result.get("dry_run") is False
    assert result.get("executed") is True
    assert len(proxy.link_calls) == 1
