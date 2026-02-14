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
