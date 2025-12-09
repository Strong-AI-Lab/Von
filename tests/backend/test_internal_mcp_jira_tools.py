"""Smoke tests for Jira MCP integration in the internal MCP catalogue.

These tests avoid hitting the real Jira MCP server and only verify catalogue
registration and basic validation behaviour.
"""

from src.backend.integrations.internal_mcp.catalogue import (
    _jira_search,
    _jira_get_issue,
    _jira_add_comment,
    _jira_transition_issue,
    build_default_catalogue,
)


def test_jira_methods_registered_in_catalogue():
    catalogue = build_default_catalogue()
    names = set(catalogue.list_methods())
    assert "jira_search" in names
    assert "jira_get_issue" in names
    assert "jira_add_comment" in names
    assert "jira_transition" in names


def test_jira_handlers_require_minimum_fields():
    # Missing required params should return a friendly error and not raise
    err_search = _jira_search(jql=None)
    assert err_search.get("success") is False
    assert "jql" in err_search.get("error", "")

    err_issue = _jira_get_issue(issue_key=None)
    assert err_issue.get("success") is False
    assert "issue_key" in err_issue.get("error", "")

    err_comment = _jira_add_comment(issue_key=None, comment=None)
    assert err_comment.get("success") is False
    err_msg = err_comment.get("error", "")
    assert "issue_key" in err_msg and "comment" in err_msg

    err_transition = _jira_transition_issue(issue_key=None, transition_id=None)
    assert err_transition.get("success") is False
    err_msg = err_transition.get("error", "")
    assert "issue_key" in err_msg and "transition_id" in err_msg
