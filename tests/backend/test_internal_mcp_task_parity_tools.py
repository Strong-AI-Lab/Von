"""Gateway-level tests for Jira-style task MCP parity operations.

These tests exercise handlers through InternalMCPGateway.invoke() and verify
that both success and error payloads remain compatible with declared output
schemas.
"""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.schemas import validate_payload
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _assert_schema_conformance(gateway: InternalMCPGateway, method: str, payload: dict) -> None:
    definition = gateway.get_method_definition(method)
    assert definition is not None
    assert definition.output_schema is not None
    ok, errors = validate_payload(definition.output_schema, payload)
    assert ok, f"{method} output schema mismatch: {errors}"


def test_task_parity_methods_registered_in_catalogue():
    methods = set(build_default_catalogue().list_methods())
    expected = {
        "task_search",
        "task_import_jira_issues",
        "task_update_fields",
        "task_get_transitions",
        "task_transition",
        "task_unassign",
        "task_set_parent",
        "task_create_subtask",
        "task_link",
        "task_unlink",
        "task_add_comment",
        "task_list_comments",
        "task_add_attachment",
        "task_list_attachments",
        "task_add_worklog",
        "task_list_worklog",
        "task_get_history",
        "task_bulk_update",
    }
    missing = sorted(expected - methods)
    assert not missing, f"Missing expected task parity methods: {missing}"


def test_task_import_jira_issues_gateway_dry_run_and_schema(monkeypatch):
    gateway = _build_gateway()

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Imported issue",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "labels": ["migration"],
                    "issuelinks": [],
                },
            }

        async def search(
            self,
            *,
            jql: str,  # noqa: ARG002
            max_results=None,  # noqa: ARG002
            start_at=None,  # noqa: ARG002
            next_page_token=None,  # noqa: ARG002
            fields=None,  # noqa: ARG002
        ):
            return {"issues": [{"key": "JVNAUTOSCI-3001"}]}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    payload = gateway.invoke(
        "task_import_jira_issues",
        {"issue_keys": ["JVNAUTOSCI-3001"], "dry_run": True},
    ).payload
    assert payload.get("success") is True
    assert payload.get("dry_run") is True
    assert payload.get("summary", {}).get("total_issues") == 1
    _assert_schema_conformance(gateway, "task_import_jira_issues", payload)


def test_task_import_jira_issues_gateway_accepts_issue_objects_from_discovery(monkeypatch):
    gateway = _build_gateway()
    fetched_issue_keys: list[str] = []

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            fetched_issue_keys.append(issue_key)
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Imported issue",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "project": {"key": "JVNAUTOSCI", "name": "JVNAUTOSCI Project"},
                    "issuelinks": [],
                },
            }

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    payload = gateway.invoke(
        "task_import_jira_issues",
        {"issue_keys": [{"key": "JVNAUTOSCI-3002"}], "dry_run": True},
    ).payload
    assert payload.get("success") is True
    assert payload.get("summary", {}).get("total_issues") == 1
    assert fetched_issue_keys == ["JVNAUTOSCI-3002"]
    _assert_schema_conformance(gateway, "task_import_jira_issues", payload)


def test_task_transition_gateway_success_and_error_schema(monkeypatch):
    gateway = _build_gateway()

    def _fake_transition_task(
        task_concept_id: str,
        *,
        transition_id=None,
        to_status=None,
        actor_concept_id=None,
    ):
        return {
            "task_concept_id": task_concept_id,
            "from_status": "pending",
            "to_status": to_status or "in_progress",
            "transition": {
                "transition_id": transition_id or "start_progress",
                "to_status": to_status or "in_progress",
            },
            "task": {"task_concept_id": task_concept_id, "status": to_status or "in_progress"},
            "actor": actor_concept_id,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.transition_task",
        _fake_transition_task,
    )

    success_payload = gateway.invoke(
        "task_transition",
        {"task_concept_id": "#V#task_1", "to_status": "in_progress"},
    ).payload
    assert success_payload.get("success") is True
    assert success_payload.get("task", {}).get("status") == "in_progress"
    _assert_schema_conformance(gateway, "task_transition", success_payload)

    error_payload = gateway.invoke("task_transition", {"task_concept_id": "#V#task_1"}).payload
    assert error_payload.get("success") is False
    assert error_payload.get("error_code") == "MISSING_PARAM"
    _assert_schema_conformance(gateway, "task_transition", error_payload)


def test_task_link_gateway_success_and_error_schema(monkeypatch):
    gateway = _build_gateway()
    from src.backend.services.task_management_service import InvalidTaskDataError

    def _fake_link_tasks(
        source_task_concept_id: str,
        target_task_concept_id: str,
        *,
        link_type: str,
        actor_concept_id=None,
    ):
        if link_type == "unsupported":
            raise InvalidTaskDataError("Unsupported link_type")
        return {
            "source_task_concept_id": source_task_concept_id,
            "target_task_concept_id": target_task_concept_id,
            "link_type": link_type,
            "linked": True,
            "actor": actor_concept_id,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.link_tasks",
        _fake_link_tasks,
    )

    success_payload = gateway.invoke(
        "task_link",
        {
            "source_task_concept_id": "#V#task_1",
            "target_task_concept_id": "#V#task_2",
            "link_type": "depends_on",
        },
    ).payload
    assert success_payload.get("success") is True
    assert success_payload.get("linked") is True
    _assert_schema_conformance(gateway, "task_link", success_payload)

    error_payload = gateway.invoke(
        "task_link",
        {
            "source_task_concept_id": "#V#task_1",
            "target_task_concept_id": "#V#task_2",
            "link_type": "unsupported",
        },
    ).payload
    assert error_payload.get("success") is False
    assert error_payload.get("error_code") == "INVALID_DATA"
    _assert_schema_conformance(gateway, "task_link", error_payload)


def test_task_comment_gateway_success_and_error_schema(monkeypatch):
    gateway = _build_gateway()

    def _fake_add_task_comment(task_concept_id: str, *, body: str, author_concept_id=None):
        return {
            "comment_id": "comment_abc123",
            "body": body,
            "author_concept_id": author_concept_id,
            "task_concept_id": task_concept_id,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.add_task_comment",
        _fake_add_task_comment,
    )

    success_payload = gateway.invoke(
        "task_add_comment",
        {"task_concept_id": "#V#task_1", "body": "Looks good"},
    ).payload
    assert success_payload.get("success") is True
    assert success_payload.get("comment", {}).get("comment_id") == "comment_abc123"
    _assert_schema_conformance(gateway, "task_add_comment", success_payload)

    error_payload = gateway.invoke("task_add_comment", {"task_concept_id": "#V#task_1"}).payload
    assert error_payload.get("success") is False
    assert error_payload.get("error_code") == "MISSING_PARAM"
    _assert_schema_conformance(gateway, "task_add_comment", error_payload)


def test_task_attachment_gateway_success_and_error_schema(monkeypatch):
    gateway = _build_gateway()

    def _fake_add_task_attachment(
        task_concept_id: str,
        *,
        filename: str,
        uri: str,
        media_type=None,
        size_bytes=None,
        added_by_concept_id=None,
        note=None,
    ):
        return {
            "attachment_id": "attachment_abc123",
            "filename": filename,
            "uri": uri,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "added_by_concept_id": added_by_concept_id,
            "note": note,
            "task_concept_id": task_concept_id,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.add_task_attachment",
        _fake_add_task_attachment,
    )

    success_payload = gateway.invoke(
        "task_add_attachment",
        {
            "task_concept_id": "#V#task_1",
            "filename": "spec.pdf",
            "uri": "blob://spec.pdf",
        },
    ).payload
    assert success_payload.get("success") is True
    assert success_payload.get("attachment", {}).get("attachment_id") == "attachment_abc123"
    _assert_schema_conformance(gateway, "task_add_attachment", success_payload)

    error_payload = gateway.invoke(
        "task_add_attachment",
        {"task_concept_id": "#V#task_1", "filename": "spec.pdf"},
    ).payload
    assert error_payload.get("success") is False
    assert error_payload.get("error_code") == "MISSING_PARAM"
    _assert_schema_conformance(gateway, "task_add_attachment", error_payload)


def test_task_create_gateway_supports_start_date_and_epic(monkeypatch):
    gateway = _build_gateway()

    def _fake_create_task(
        title: str,
        description: str,
        *,
        assignee_concept_id=None,
        originating_session_id=None,
        created_by_concept_id=None,
        start_date=None,
        due_date=None,
        epic_task_concept_id=None,
        components=None,
        fix_versions=None,
        sprint_values=None,
        backlog_rank=None,
        priority="medium",
        organisation_concept_id=None,
    ):
        return {
            "task_concept_id": "#V#task_123",
            "title": title,
            "description": description,
            "status": "pending",
            "priority": priority,
            "assignee_concept_id": assignee_concept_id,
            "created_by_concept_id": created_by_concept_id,
            "originating_conversation_id": originating_session_id,
            "start_date": start_date.isoformat() if start_date is not None else None,
            "due_date": due_date.isoformat() if due_date is not None else None,
            "epic_task_concept_id": epic_task_concept_id,
            "components": components,
            "fix_versions": fix_versions,
            "sprint_values": sprint_values,
            "backlog_rank": backlog_rank,
            "organisation_concept_id": organisation_concept_id,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.create_task",
        _fake_create_task,
    )

    payload = gateway.invoke(
        "task_create",
        {
            "title": "Task with dates",
            "description": "Details",
            "start_date": "2026-03-01T10:00:00Z",
            "due_date": "2026-03-05T10:00:00Z",
            "epic_task_concept_id": "#V#task_epic_1",
        },
    ).payload
    assert payload.get("success") is True
    assert payload.get("start_date") == "2026-03-01T10:00:00+00:00"
    assert payload.get("due_date") == "2026-03-05T10:00:00+00:00"
    assert payload.get("epic_task_concept_id") == "#V#task_epic_1"
    _assert_schema_conformance(gateway, "task_create", payload)


def test_task_search_gateway_supports_start_and_epic_filters(monkeypatch):
    gateway = _build_gateway()
    definition = gateway.get_method_definition("task_search")
    assert definition is not None
    assert definition.input_schema is not None
    assert "start_from" in definition.input_schema.optional
    assert "start_to" in definition.input_schema.optional
    assert "epic_task_concept_id" in definition.input_schema.optional
    assert "has_epic" in definition.input_schema.optional
    assert "components" in definition.input_schema.optional
    assert "fix_versions" in definition.input_schema.optional
    assert "sprint_values" in definition.input_schema.optional
    assert "backlog_rank" in definition.input_schema.optional
    assert "has_backlog_rank" in definition.input_schema.optional

    captured: dict = {}

    def _fake_search_tasks(**kwargs):
        captured.update(kwargs)
        return {
            "tasks": [],
            "total": 0,
            "count": 0,
            "offset": kwargs.get("offset", 0),
            "limit": kwargs.get("limit", 50),
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.search_tasks",
        _fake_search_tasks,
    )

    payload = gateway.invoke(
        "task_search",
        {
            "start_from": "2026-03-01T00:00:00Z",
            "start_to": "2026-03-15T00:00:00Z",
            "epic_task_concept_id": "#V#task_epic_1",
            "has_epic": True,
            "components": ["Workflow Engine"],
            "fix_versions": ["R1"],
            "sprint_values": ["Sprint 6"],
            "backlog_rank": "0|i00123:",
            "has_backlog_rank": True,
        },
    ).payload
    assert payload.get("success") is True
    assert captured.get("start_from") == "2026-03-01T00:00:00Z"
    assert captured.get("start_to") == "2026-03-15T00:00:00Z"
    assert captured.get("epic_task_concept_id") == "#V#task_epic_1"
    assert captured.get("has_epic") is True
    assert captured.get("components") == ["Workflow Engine"]
    assert captured.get("fix_versions") == ["R1"]
    assert captured.get("sprint_values") == ["Sprint 6"]
    assert captured.get("backlog_rank") == "0|i00123:"
    assert captured.get("has_backlog_rank") is True
    _assert_schema_conformance(gateway, "task_search", payload)
