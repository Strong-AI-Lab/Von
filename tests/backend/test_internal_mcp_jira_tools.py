"""Smoke tests for Jira MCP integration in the internal MCP catalogue.

These tests avoid hitting the real Jira MCP server and only verify catalogue
registration and basic validation behaviour.
"""

from src.backend.integrations.internal_mcp.catalogue import (
    _jira_search,
    _jira_get_issue,
    _jira_get_transitions,
    _jira_add_comment,
    _jira_create_issue,
    _jira_update_issue,
    _jira_link_issue,
    _jira_transition_issue,
    build_default_catalogue,
)


def test_jira_methods_registered_in_catalogue():
    catalogue = build_default_catalogue()
    names = set(catalogue.list_methods())
    assert "jira_search" in names
    assert "jira_get_issue" in names
    assert "jira_get_transitions" in names
    assert "jira_add_comment" in names
    assert "jira_create_issue" in names
    assert "jira_update_issue" in names
    assert "jira_link_issue" in names
    assert "jira_transition" in names
    assert "jira_get_myself" in names
    assert "jira_get_auth_config" in names


def test_jira_handlers_require_minimum_fields():
    # Missing required params should return a friendly error and not raise
    err_search = _jira_search(jql=None)
    assert err_search.get("success") is False
    assert "jql" in err_search.get("error", "")

    err_issue = _jira_get_issue(issue_key=None)
    assert err_issue.get("success") is False
    assert "issue_key" in err_issue.get("error", "")

    err_transitions = _jira_get_transitions(issue_key=None)
    assert err_transitions.get("success") is False
    assert "issue_key" in err_transitions.get("error", "")

    err_comment = _jira_add_comment(issue_key=None, comment=None)
    assert err_comment.get("success") is False
    err_msg = err_comment.get("error", "")
    assert "issue_key" in err_msg and "comment" in err_msg

    err_transition = _jira_transition_issue(issue_key=None, transition_id=None)
    assert err_transition.get("success") is False
    err_msg = err_transition.get("error", "")
    assert "issue_key" in err_msg and "transition_id" in err_msg

    err_create = _jira_create_issue(project_key=None, issue_type=None, summary=None)
    assert err_create.get("success") is False
    assert "project_key" in err_create.get("error", "")

    err_update = _jira_update_issue(issue_key=None, update_fields=None)
    assert err_update.get("success") is False
    assert "issue_key" in err_update.get("error", "")

    err_link = _jira_link_issue(
        inward_issue_key=None, outward_issue_key=None, link_type=None
    )
    assert err_link.get("success") is False
    assert "inward_issue_key" in err_link.get("error", "")


def test_jira_write_tools_allowlist_and_dry_run_defaults():
    # Default allow-list includes JVNAUTOSCI.
    ok_create = _jira_create_issue(
        project_key="JVNAUTOSCI",
        issue_type="Task",
        summary="Test create",
    )
    assert ok_create.get("success") is True
    assert ok_create.get("dry_run") is True
    assert ok_create.get("executed") is False
    assert (
        ok_create.get("proposed_payload", {})
        .get("fields", {})
        .get("project", {})
        .get("key")
        == "JVNAUTOSCI"
    )

    bad_create = _jira_create_issue(
        project_key="OTHER",
        issue_type="Task",
        summary="Not allowed",
    )
    assert bad_create.get("success") is False
    assert bad_create.get("error_code") == "project_not_allowlisted"

    ok_update = _jira_update_issue(
        issue_key="JVNAUTOSCI-123",
        update_fields={"summary": "Updated"},
    )
    assert ok_update.get("success") is True
    assert ok_update.get("dry_run") is True
    assert ok_update.get("executed") is False

    bad_update = _jira_update_issue(
        issue_key="OTHER-1",
        update_fields={"summary": "Updated"},
    )
    assert bad_update.get("success") is False
    assert bad_update.get("error_code") == "project_not_allowlisted"

    ok_link = _jira_link_issue(
        inward_issue_key="JVNAUTOSCI-1",
        outward_issue_key="JVNAUTOSCI-2",
        link_type="Relates",
    )
    assert ok_link.get("success") is True
    assert ok_link.get("dry_run") is True
    assert ok_link.get("executed") is False

    bad_link = _jira_link_issue(
        inward_issue_key="JVNAUTOSCI-1",
        outward_issue_key="OTHER-2",
        link_type="Relates",
    )
    assert bad_link.get("success") is False
    assert bad_link.get("error_code") == "project_not_allowlisted"


def test_jira_write_tools_require_approval_when_not_dry_run():
    # Should fail closed before any external call.
    err = _jira_create_issue(
        project_key="JVNAUTOSCI",
        issue_type="Task",
        summary="Real create",
        dry_run=False,
    )
    assert err.get("success") is False
    assert err.get("error_code") == "approval_required"


def test_jira_get_transitions_proxy_error_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
    from src.backend.integrations.internal_mcp.jira_proxy_mcp import JiraProxyError

    class _FailingProxy:
        async def get_transitions(self, *, issue_key: str):  # noqa: ARG002
            raise JiraProxyError("proxy unavailable")

    async def _fake_get_jira_proxy():
        return _FailingProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke("jira_get_transitions", {"issue_key": "JVNAUTOSCI-1141"})
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("error_code") == "jira_proxy_error"
