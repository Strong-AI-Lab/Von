"""Smoke tests for Jira MCP integration in the internal MCP catalogue.

These tests avoid hitting the real Jira MCP server and only verify catalogue
registration and basic validation behaviour.
"""

import base64

from src.backend.integrations.internal_mcp.catalogue import (
    _jira_search,
    _jira_get_issue,
    _jira_get_bulk_operation_progress,
    _jira_get_transitions,
    _jira_add_comment,
    _jira_add_attachment,
    _jira_create_issue,
    _jira_move_issue,
    _jira_update_issue,
    _jira_link_issue,
    _jira_delete_issue_link,
    _jira_transition_issue,
    build_default_catalogue,
)


def test_jira_methods_registered_in_catalogue():
    catalogue = build_default_catalogue()
    names = set(catalogue.list_methods())
    assert "jira_search" in names
    assert "jira_get_issue" in names
    assert "jira_get_project_issue_types" in names
    assert "jira_get_bulk_operation_progress" in names
    assert "jira_get_transitions" in names
    assert "jira_add_comment" in names
    assert "jira_add_attachment" in names
    assert "jira_create_issue" in names
    assert "jira_update_issue" in names
    assert "jira_move_issue" in names
    assert "jira_link_issue" in names
    assert "jira_delete_issue_link" in names
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

    err_bulk_progress = _jira_get_bulk_operation_progress(task_id=None)
    assert err_bulk_progress.get("success") is False
    assert "task_id" in err_bulk_progress.get("error", "")

    err_transitions = _jira_get_transitions(issue_key=None)
    assert err_transitions.get("success") is False
    assert "issue_key" in err_transitions.get("error", "")

    err_comment = _jira_add_comment(issue_key=None, comment=None)
    assert err_comment.get("success") is False
    err_msg = err_comment.get("error", "")
    assert "issue_key" in err_msg and "comment" in err_msg

    err_attachment = _jira_add_attachment(
        issue_key=None,
        filename=None,
        content_base64=None,
        mime_type=None,
    )
    assert err_attachment.get("success") is False
    assert "issue_key" in err_attachment.get("error", "")

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

    err_move = _jira_move_issue(issue_key=None)
    assert err_move.get("success") is False
    assert "issue_key" in err_move.get("error", "")

    err_link = _jira_link_issue(link_type="Blocks")
    assert err_link.get("success") is False
    assert "link_type" in err_link.get("error", "")

    err_delete = _jira_delete_issue_link(issue_link_id=None)
    assert err_delete.get("success") is False
    assert "issue_link_id" in err_delete.get("error", "")


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
            {"id": "10123", "name": "Epic", "subtask": False},
            {"id": "10124", "name": "Subtask", "subtask": True},
        ],
        "project_issue_types": [
            {"id": "10122", "name": "Task", "subtask": False},
            {"id": "10123", "name": "Epic", "subtask": False},
            {"id": "10124", "name": "Subtask", "subtask": True},
        ],
    }


def _jira_issue_payload(
    issue_key: str,
    *,
    issue_type_id: str,
    issue_type_name: str,
    subtask: bool,
    project_key: str = "JVNAUTOSCI",
    parent_issue_key: str | None = None,
) -> dict:
    fields = {
        "project": {"key": project_key},
        "issuetype": {
            "id": issue_type_id,
            "name": issue_type_name,
            "subtask": subtask,
        },
    }
    if isinstance(parent_issue_key, str):
        fields["parent"] = {"key": parent_issue_key}
    return {"key": issue_key, "fields": fields}


def test_jira_write_tools_allowlist_and_dry_run_defaults(monkeypatch):
    class _FakeProxy:
        async def get_project_issue_types(self, *, project_key: str):
            assert project_key == "JVNAUTOSCI"
            return _team_managed_project_issue_type_context(project_key)

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    # Default allow-list includes JVNAUTOSCI.
    ok_create = _jira_create_issue(
        project_key="JVNAUTOSCI",
        issue_type="Task",
        summary="Test create",
        components=[
            "Platform",
            {"name": " UI "},
            {"id": 10001},
            {"name": "   "},
            {"id": "   "},
        ],
    )
    assert ok_create.get("success") is True
    assert ok_create.get("dry_run") is True
    assert ok_create.get("executed") is False
    assert ok_create.get("issue_type_preflight", {}).get(
        "available_issue_type_names"
    ) == ["Task", "Epic", "Subtask"]
    assert (
        ok_create.get("proposed_payload", {})
        .get("fields", {})
        .get("project", {})
        .get("key")
        == "JVNAUTOSCI"
    )
    assert ok_create.get("proposed_payload", {}).get("fields", {}).get("components") == [
        {"name": "Platform"},
        {"name": "UI"},
        {"id": "10001"},
    ]

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
    assert ok_link.get("source_issue_key") == "JVNAUTOSCI-1"
    assert ok_link.get("target_issue_key") == "JVNAUTOSCI-2"

    ok_link_semantic = _jira_link_issue(
        source_issue_key="JVNAUTOSCI-10",
        target_issue_key="JVNAUTOSCI-11",
        link_type="Blocks",
    )
    assert ok_link_semantic.get("success") is True
    assert ok_link_semantic.get("dry_run") is True
    assert ok_link_semantic.get("executed") is False
    proposed = ok_link_semantic.get("proposed_payload", {})
    assert proposed.get("inwardIssue", {}).get("key") == "JVNAUTOSCI-10"
    assert proposed.get("outwardIssue", {}).get("key") == "JVNAUTOSCI-11"

    err_conflicting_pair = _jira_link_issue(
        source_issue_key="JVNAUTOSCI-10",
        target_issue_key="JVNAUTOSCI-11",
        outward_issue_key="JVNAUTOSCI-99",
        link_type="Blocks",
    )
    assert err_conflicting_pair.get("success") is False
    assert err_conflicting_pair.get("error_code") == "CONFLICTING_PARAMS"

    bad_link = _jira_link_issue(
        inward_issue_key="JVNAUTOSCI-1",
        outward_issue_key="OTHER-2",
        link_type="Relates",
    )
    assert bad_link.get("success") is False
    assert bad_link.get("error_code") == "project_not_allowlisted"

    ok_delete = _jira_delete_issue_link(
        issue_link_id="12345",
        source_issue_key="JVNAUTOSCI-1",
    )
    assert ok_delete.get("success") is True
    assert ok_delete.get("dry_run") is True
    assert ok_delete.get("executed") is False
    assert ok_delete.get("issue_link_id") == "12345"

    bad_delete = _jira_delete_issue_link(
        issue_link_id="12345",
        source_issue_key="OTHER-2",
    )
    assert bad_delete.get("success") is False
    assert bad_delete.get("error_code") == "project_not_allowlisted"


def test_jira_create_issue_reports_unavailable_project_issue_type(monkeypatch):
    class _FakeProxy:
        async def get_project_issue_types(self, *, project_key: str):
            assert project_key == "JVNAUTOSCI"
            return _team_managed_project_issue_type_context(project_key)

        async def create_issue(self, *, payload):  # pragma: no cover - defensive
            raise AssertionError(f"create_issue should not be called: {payload}")

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    result = _jira_create_issue(
        project_key="JVNAUTOSCI",
        issue_type="Bug",
        summary="Need a bug",
    )

    assert result.get("success") is False
    assert result.get("error_code") == "jira_issue_type_not_available_for_project"
    assert result.get("error_details", {}).get("project_style") == "next-gen"
    assert result.get("error_details", {}).get("is_team_managed") is True
    assert result.get("error_details", {}).get("available_issue_types") == [
        "Task",
        "Epic",
        "Subtask",
    ]


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


def test_jira_get_issue_supports_expand_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def get_issue(
            self,
            *,
            issue_key: str,
            fields=None,
            expand=None,
        ):
            assert issue_key == "JVNAUTOSCI-1141"
            assert fields == ["summary", "status"]
            assert expand == ["changelog"]
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Renderer applicability task",
                    "status": {"name": "Done"},
                },
                "changelog": {"histories": []},
            }

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "jira_get_issue",
        {
            "issue_key": "JVNAUTOSCI-1141",
            "fields": ["summary", "status"],
            "expand": ["changelog"],
        },
    )
    payload = result.payload
    assert payload.get("key") == "JVNAUTOSCI-1141"
    assert payload.get("fields", {}).get("summary") == "Renderer applicability task"
    assert payload.get("changelog") == {"histories": []}


def test_jira_get_project_issue_types_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def get_project_issue_types(self, *, project_key: str):
            assert project_key == "JVNAUTOSCI"
            return _team_managed_project_issue_type_context(project_key)

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "jira_get_project_issue_types",
        {"project_key": "JVNAUTOSCI"},
    )
    payload = result.payload
    assert payload.get("success") is True
    assert payload.get("project_key") == "JVNAUTOSCI"
    assert payload.get("project_style") == "next-gen"
    assert payload.get("available_issue_type_names") == ["Task", "Epic", "Subtask"]


def test_jira_move_issue_dry_run_infers_subtask_from_parent(monkeypatch):
    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None, expand=None):
            assert fields == ["project", "issuetype", "parent"]
            assert expand is None
            if issue_key == "JVNAUTOSCI-1874":
                return _jira_issue_payload(
                    issue_key,
                    issue_type_id="10122",
                    issue_type_name="Task",
                    subtask=False,
                )
            if issue_key == "JVNAUTOSCI-1873":
                return _jira_issue_payload(
                    issue_key,
                    issue_type_id="10122",
                    issue_type_name="Task",
                    subtask=False,
                )
            raise AssertionError(f"unexpected issue_key: {issue_key}")

        async def get_project_issue_types(self, *, project_key: str):
            assert project_key == "JVNAUTOSCI"
            return _team_managed_project_issue_type_context(project_key)

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    result = _jira_move_issue(
        issue_key="JVNAUTOSCI-1874",
        target_parent_issue_key="JVNAUTOSCI-1873",
    )

    assert result.get("success") is True
    assert result.get("dry_run") is True
    assert result.get("executed") is False
    assert result.get("target_issue_type_name") == "Subtask"
    assert result.get("target_issue_type_id") == "10124"
    assert result.get("target_parent_issue_key") == "JVNAUTOSCI-1873"
    assert result.get("move_preflight", {}).get("mapping_key") == (
        "JVNAUTOSCI,10124,JVNAUTOSCI-1873"
    )
    assert (
        result.get("proposed_payload", {})
        .get("targetToSourcesMapping", {})
        .get("JVNAUTOSCI,10124,JVNAUTOSCI-1873", {})
        .get("issueIdsOrKeys")
        == ["JVNAUTOSCI-1874"]
    )


def test_jira_get_bulk_operation_progress_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def get_bulk_operation_progress(self, *, task_id: str):
            assert task_id == "9001"
            return {
                "taskId": "9001",
                "status": "RUNNING",
                "progressPercent": 65,
            }

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "jira_get_bulk_operation_progress",
        {"task_id": "9001"},
    )
    payload = result.payload
    assert payload.get("success") is True
    assert payload.get("task_id") == "9001"
    assert payload.get("status") == "RUNNING"
    assert payload.get("terminal") is False


def test_jira_move_issue_await_completion_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    progress_states = [
        {
            "taskId": "9001",
            "status": "RUNNING",
            "progressPercent": 10,
        },
        {
            "taskId": "9001",
            "status": "COMPLETE",
            "progressPercent": 100,
        },
    ]

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None, expand=None):
            assert fields == ["project", "issuetype", "parent"]
            assert expand is None
            if issue_key == "JVNAUTOSCI-1874":
                return _jira_issue_payload(
                    issue_key,
                    issue_type_id="10122",
                    issue_type_name="Task",
                    subtask=False,
                )
            if issue_key == "JVNAUTOSCI-1873":
                return _jira_issue_payload(
                    issue_key,
                    issue_type_id="10122",
                    issue_type_name="Task",
                    subtask=False,
                )
            raise AssertionError(f"unexpected issue_key: {issue_key}")

        async def get_project_issue_types(self, *, project_key: str):
            assert project_key == "JVNAUTOSCI"
            return _team_managed_project_issue_type_context(project_key)

        async def move_issue(self, *, payload):
            mapping = payload.get("targetToSourcesMapping", {})
            assert "JVNAUTOSCI,10124,JVNAUTOSCI-1873" in mapping
            return {"taskId": "9001"}

        async def get_bulk_operation_progress(self, *, task_id: str):
            assert task_id == "9001"
            return progress_states.pop(0)

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "jira_move_issue",
        {
            "issue_key": "JVNAUTOSCI-1874",
            "target_parent_issue_key": "JVNAUTOSCI-1873",
            "dry_run": False,
            "approved": True,
            "await_completion": True,
            "poll_interval_seconds": 0.01,
            "timeout_seconds": 1.0,
        },
    )
    payload = result.payload
    assert payload.get("success") is True
    assert payload.get("dry_run") is False
    assert payload.get("executed") is True
    assert payload.get("bulk_task_id") == "9001"
    assert payload.get("target_issue_type_name") == "Subtask"
    assert payload.get("awaited_completion") is True
    assert payload.get("bulk_progress", {}).get("status") == "COMPLETE"
    assert payload.get("bulk_progress", {}).get("terminal") is True


def test_jira_delete_issue_link_success_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def delete_issue_link(self, *, issue_link_id: str):
            assert issue_link_id == "10001"
            return {"status_code": 204}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "jira_delete_issue_link",
        {
            "issue_link_id": "10001",
            "source_issue_key": "JVNAUTOSCI-1152",
            "target_issue_key": "JVNAUTOSCI-1150",
            "dry_run": False,
            "approved": True,
        },
    )
    payload = result.payload
    assert payload.get("success") is True
    assert payload.get("dry_run") is False
    assert payload.get("executed") is True
    assert payload.get("action") == "delete_issue_link"
    assert payload.get("issue_link_id") == "10001"
    assert payload.get("status_code") == 204


def test_jira_delete_issue_link_proxy_error_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
    from src.backend.integrations.internal_mcp.jira_proxy_mcp import JiraProxyError

    class _FailingProxy:
        async def delete_issue_link(self, *, issue_link_id: str):  # noqa: ARG002
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

    result = gateway.invoke(
        "jira_delete_issue_link",
        {
            "issue_link_id": "10001",
            "source_issue_key": "JVNAUTOSCI-1152",
            "dry_run": False,
            "approved": True,
        },
    )
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("error_code") == "JIRA_ERROR"


def test_jira_add_attachment_validation_rejects_disallowed_mime_type():
    payload = _jira_add_attachment(
        issue_key="JVNAUTOSCI-1141",
        filename="diagram.png",
        content_base64=base64.b64encode(b"abc").decode("ascii"),
        mime_type="application/x-msdownload",
    )
    assert payload.get("success") is False
    assert payload.get("error_code") == "unsupported_mime_type"


def test_jira_add_attachment_success_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def add_attachment(
            self,
            *,
            issue_key: str,
            filename: str,
            content_base64: str,
            mime_type: str,
            comment: str | None = None,
        ):
            assert issue_key == "JVNAUTOSCI-1141"
            assert filename == "relation_extent_table.png"
            assert content_base64
            assert mime_type == "image/png"
            assert comment == "Design screenshot"
            return {
                "attachments": [
                    {
                        "id": "10042",
                        "filename": filename,
                        "size": 3,
                        "mimeType": mime_type,
                    }
                ],
                "comment_added": True,
            }

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "jira_add_attachment",
        {
            "issue_key": "JVNAUTOSCI-1141",
            "filename": "../relation extent table.png",
            "content_base64": base64.b64encode(b"abc").decode("ascii"),
            "mime_type": "image/png",
            "comment": "Design screenshot",
        },
    )
    payload = result.payload
    assert payload.get("success") is True
    assert payload.get("issue_key") == "JVNAUTOSCI-1141"
    assert payload.get("attachment_id") == "10042"
    assert payload.get("filename") == "relation_extent_table.png"
    assert payload.get("size_bytes") == 3
    assert payload.get("content_type") == "image/png"
    assert payload.get("comment_added") is True


def test_jira_add_attachment_proxy_error_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
    from src.backend.integrations.internal_mcp.jira_proxy_mcp import JiraProxyError

    class _FailingProxy:
        async def add_attachment(self, **_kwargs):
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

    result = gateway.invoke(
        "jira_add_attachment",
        {
            "issue_key": "JVNAUTOSCI-1141",
            "filename": "diagram.png",
            "content_base64": base64.b64encode(b"abc").decode("ascii"),
            "mime_type": "image/png",
        },
    )
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("error_code") == "jira_proxy_error"


def test_jira_add_attachment_auth_error_passthrough_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _AuthErrorProxy:
        async def add_attachment(self, **_kwargs):
            return {
                "success": False,
                "status_code": 401,
                "error": "Jira API HTTP 401",
                "hint": "Unauthorised: check Atlassian email/token validity.",
            }

    async def _fake_get_jira_proxy():
        return _AuthErrorProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "jira_add_attachment",
        {
            "issue_key": "JVNAUTOSCI-1141",
            "filename": "diagram.png",
            "content_base64": base64.b64encode(b"abc").decode("ascii"),
            "mime_type": "image/png",
        },
    )
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("status_code") == 401


def test_jira_create_issue_components_through_gateway_invoke(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    class _FakeProxy:
        async def get_project_issue_types(self, *, project_key: str):
            assert project_key == "JVNAUTOSCI"
            return _team_managed_project_issue_type_context(project_key)

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "jira_create_issue",
        {
            "project_key": "JVNAUTOSCI",
            "issue_type": "Task",
            "summary": "Gateway create components test",
            "components": [
                "Backend",
                {"name": "Frontend"},
                {"id": 42},
                None,
            ],
        },
    )
    payload = result.payload
    assert payload.get("success") is True
    assert payload.get("dry_run") is True
    assert payload.get("executed") is False
    assert payload.get("issue_type_preflight", {}).get(
        "available_issue_type_names"
    ) == ["Task", "Epic", "Subtask"]
    assert (
        payload.get("proposed_payload", {}).get("fields", {}).get("components")
        == [
            {"name": "Backend"},
            {"name": "Frontend"},
            {"id": "42"},
        ]
    )
