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
