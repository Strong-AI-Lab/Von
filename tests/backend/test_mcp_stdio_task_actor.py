import json

import pytest

from src.backend.integrations.internal_mcp.gateway import (
    bind_internal_mcp_actor_context_source,
)
from src.backend.mcp_server import mcp_stdio_server as server
from src.backend.security.access_control import (
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)


@pytest.mark.asyncio
async def test_configured_task_principal_needs_no_caller_identity(monkeypatch):
    monkeypatch.setenv("VON_MCP_TASK_ACTOR_CONCEPT_ID", "#V#owner")
    monkeypatch.setenv("VON_MCP_TASK_ORGANISATION_CONCEPT_ID", "#V#org")

    async def handler(arguments):
        assert arguments["request_id"].startswith("task-mcp-")
        assert get_effective_organisation_concept_id() == "#V#org"
        return [
            server._json_text(
                {
                    "success": True,
                    "actor": get_effective_user_concept_id(),
                    "creator": arguments["creator_concept_id"],
                }
            )
        ]

    monkeypatch.setitem(server._TOOL_HANDLERS, "task_get", handler)
    original = {"task_concept_id": "KKAT-1"}
    result = await server.call_tool("task_get", original)
    assert json.loads(result[0].text) == {
        "success": True,
        "actor": "#V#owner",
        "creator": "#V#owner",
    }
    assert original == {"task_concept_id": "KKAT-1"}
    assert get_effective_user_concept_id() is None


@pytest.mark.asyncio
async def test_payload_cannot_impersonate_a_different_task_actor(monkeypatch):
    monkeypatch.setenv("VON_MCP_TASK_ACTOR_CONCEPT_ID", "#V#owner")

    async def handler(arguments):
        pytest.fail("Identity mismatch must be rejected before the task handler")

    monkeypatch.setitem(server._TOOL_HANDLERS, "task_get", handler)
    result = await server.call_tool(
        "task_get",
        {"task_concept_id": "KKAT-1", "acting_user_concept_id": "#V#someone_else"},
    )
    assert json.loads(result[0].text)["error_code"] == "task_actor_mismatch"


@pytest.mark.asyncio
async def test_project_search_does_not_add_assignee_or_creator_filters(monkeypatch):
    from src.backend.services import task_management_service as tasks

    monkeypatch.setenv("VON_MCP_TASK_ACTOR_CONCEPT_ID", "#V#owner")
    monkeypatch.setenv("VON_MCP_TASK_ORGANISATION_CONCEPT_ID", "#V#org")

    def search(**kwargs):
        assert get_effective_user_concept_id() == "#V#owner"
        assert get_effective_organisation_concept_id() == "#V#org"
        assert kwargs["project_concept_id"] == "#V#project"
        assert kwargs["assignee_concept_id"] is None
        assert kwargs["created_by_concept_id"] is None
        return {"tasks": [], "total": 0}

    monkeypatch.setattr(tasks, "search_tasks", search)
    original = {"project_concept_id": "#V#project"}
    result = await server.call_tool("task_search", original)
    assert json.loads(result[0].text)["success"]
    assert original == {"project_concept_id": "#V#project"}
    assert get_effective_user_concept_id() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument", "filter_name"),
    [
        ("user_concept_id", "assignee_concept_id"),
        ("assignee_id", "assignee_concept_id"),
        ("created_by_concept_id", "created_by_concept_id"),
        ("creator_concept_id", "created_by_concept_id"),
    ],
)
async def test_task_person_filters_do_not_replace_the_actor(
    monkeypatch, argument, filter_name
):
    from src.backend.services import task_management_service as tasks

    monkeypatch.setenv("VON_MCP_TASK_ACTOR_CONCEPT_ID", "#V#owner")

    def search(**kwargs):
        assert get_effective_user_concept_id() == "#V#owner"
        assert kwargs[filter_name] == "#V#colleague"
        return {"tasks": [], "total": 0}

    monkeypatch.setattr(tasks, "search_tasks", search)
    result = await server.call_tool("task_search", {argument: "#V#colleague"})
    assert json.loads(result[0].text)["success"]
    assert get_effective_user_concept_id() is None


@pytest.mark.asyncio
async def test_task_search_still_rejects_a_conflicting_actor(monkeypatch):
    from src.backend.services import task_management_service as tasks

    monkeypatch.setenv("VON_MCP_TASK_ACTOR_CONCEPT_ID", "#V#owner")

    def search(**kwargs):
        pytest.fail("A conflicting actor must not reach canonical search")

    monkeypatch.setattr(tasks, "search_tasks", search)
    result = await server.call_tool(
        "task_search",
        {
            "created_by_concept_id": "#V#colleague",
            "acting_user_concept_id": "#V#colleague",
        },
    )
    assert json.loads(result[0].text)["error_code"] == "task_actor_mismatch"


@pytest.mark.asyncio
async def test_project_search_returns_summaries_when_import_details_exceed_transport(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setenv("VON_MCP_TASK_ACTOR_CONCEPT_ID", "#V#owner")
    monkeypatch.setenv("VON_MCP_STDIO_MAX_RESPONSE_CHARS", "10000")
    task = {
        "task_concept_id": "#V#task",
        "title": "Imported task",
        "description": "Retained description. " * 1000,
        "project_concept_id": "#V#project",
        "external_references": {
            "jira": {
                "external_id": "TEST-1",
                "source_archive_history": [{"retained_source": "x" * 10000}],
            }
        },
    }
    monkeypatch.setattr(
        catalogue,
        "_task_search",
        lambda **kwargs: {"success": True, "tasks": [task], "total": 1},
    )
    result = await server.call_tool("task_search", {"project_concept_id": "#V#project"})
    payload = json.loads(result[0].text)
    assert payload["success"] and payload["total"] == 1
    assert payload["detail_tool"] == "task_get"
    summary = payload["tasks"][0]
    assert summary["task_concept_id"] == task["task_concept_id"]
    assert summary["project_concept_id"] == "#V#project"
    assert summary["external_references"]["jira"]["external_id"] == "TEST-1"
    assert summary["description_truncated"]
    assert task["description"].startswith(summary["description_preview"])
    assert "source_archive_history" in task["external_references"]["jira"]
    assert len(task["description"]) > 10000


@pytest.mark.asyncio
async def test_configured_operator_does_not_replace_an_inherited_task_context(
    monkeypatch,
):
    monkeypatch.setenv("VON_MCP_TASK_ACTOR_CONCEPT_ID", "#V#owner")

    async def handler(arguments):
        assert "creator_concept_id" not in arguments
        return [server._json_text({"success": True})]

    monkeypatch.setitem(server._TOOL_HANDLERS, "task_get", handler)
    with bind_internal_mcp_actor_context_source("tool_payload_fallback"):
        result = await server.call_tool("task_get", {"task_concept_id": "KKAT-1"})
    assert json.loads(result[0].text)["success"]


@pytest.mark.parametrize("organisation", [None, "#V#org"])
def test_task_effects_support_personal_and_organisation_owners(
    monkeypatch, organisation
):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service as tasks

    monkeypatch.setattr(
        tasks,
        "get_task",
        lambda key: {
            "task_concept_id": key,
            "created_by_concept_id": "#V#owner",
            "organisation_concept_id": organisation,
        },
    )
    with override_current_actor("#V#owner", organisation):
        _scope, task, error = catalogue._authorise_task_actor_mutation(
            {"task_concept_id": "#V#task"}, surface="task_add_comment"
        )
    assert error is None and task["task_concept_id"] == "#V#task"


def test_local_task_operator_still_requires_canonical_task_access(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue, gateway
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service as tasks

    def denied(key):
        raise tasks.TaskNotFoundError("Task not found")

    monkeypatch.setattr(tasks, "get_task", denied)
    with (
        override_current_actor("#V#owner", None),
        gateway.bind_internal_mcp_actor_context_source(
            gateway.INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
            preexisting_actor_context=("#V#owner", None),
        ),
    ):
        _scope, task, error = catalogue._authorise_task_actor_mutation(
            {"task_concept_id": "#V#private_to_someone_else"},
            surface="task_add_comment",
        )
    assert task is None and error["error_code"] == "NOT_FOUND"


def test_ordinary_task_effect_cannot_cross_organisation_or_owner(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import task_management_service as tasks

    monkeypatch.setattr(
        tasks,
        "get_task",
        lambda key: {
            "task_concept_id": key,
            "created_by_concept_id": "#V#other",
            "organisation_concept_id": "#V#org",
        },
    )
    with override_current_actor("#V#owner", None):
        _, task, error = catalogue._authorise_task_actor_mutation(
            {"task_concept_id": "#V#task"}, surface="task_add_comment"
        )
    assert task is None and error["error_code"] == "task_actor_scope_denied"
